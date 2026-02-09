import datetime
import re
import typing
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import beanquery
from beancount.core import convert, data, prices
from beancount.core.amount import Amount
from beancount.core.data import Account, Custom, Meta
from beancount.core.inventory import Inventory
from beancount.parser import options
from dateutil.relativedelta import relativedelta
from fava.beans.types import BeancountOptions
from fava.helpers import BeancountError

from fava_envelope.modules import convert as envelope_convert
from fava_envelope.modules import directive
from fava_envelope.modules.budgets import Envelope, Income
from fava_envelope.modules.year_month import YearMonth, months_between


class BeancountEnvelope:
    """Class responsible for generating the budget tables."""

    start_date: YearMonth
    end_date: YearMonth  # Inclusive range

    # Regex of beancount account and corresponding budgeting account
    mappings: list[tuple[re.Pattern, str]]
    # A dict from budgeting account to the set of beancount accounts included in it
    mappings_used: defaultdict[str, set[str]]

    # The list of currencies that the envelope allows with no conversions, first
    # element of the list is the primary currency.
    operating_currency: list[str]

    etype: str  # Custom directive type used by fava-envelope

    envelope_df: defaultdict[
        Account,
        defaultdict[YearMonth, Envelope],
    ]
    income_df: defaultdict[
        YearMonth,
        Income,
    ]
    # Track X-flagged (scheduled/future) transactions: (account, month) -> (count, total_inventory)
    scheduled_transactions: defaultdict[Account, defaultdict[YearMonth, tuple[int, Inventory]]]

    price_map: prices.PriceMap

    available_months: list[YearMonth]

    include_starting_balance: bool

    errors: list[BeancountError]

    def __init__(
        self,
        entries: list[Any],
        options_map: BeancountOptions,
        currency: str | None,
        errors: list[BeancountError],
    ):
        self.entries = entries
        self.options_map = options_map
        self.negative_rollover = False
        self.months_ahead = 0
        self.errors = errors
        self.available_months = []

        self.etype = "envelope"

        (
            self.start_date,
            self.budget_accounts,
            self.mappings,
            self.income_accounts,
            self.months_ahead,
            self.operating_currency,
        ) = self._find_envelope_settings()
        self.mappings_used = defaultdict(set)

        if currency:
            self.operating_currency = [currency]
        elif len(self.operating_currency) == 0:
            self.operating_currency = self._find_fallback_currency(options_map)

        decimal_precison = "0.00"
        self.Q = Decimal(decimal_precison)

        # TODO should be able to assert errors

        self.end_date = YearMonth.of_date(
            datetime.date.today() + relativedelta(months=+self.months_ahead)
        )

        self.price_map = prices.build_price_map(entries)
        self.acctypes = options.get_account_types(options_map)

    def _find_fallback_currency(self, options_map: BeancountOptions) -> list[str]:
        default_currency = "USD"
        opt_currency = options_map.get("operating_currency")
        return opt_currency if opt_currency else [default_currency]

    def _find_envelope_settings(
        self,
    ) -> tuple[YearMonth, list, list, list, int, list[str]]:
        extension_init_date: YearMonth = YearMonth(1970, 1)
        start_date: YearMonth | None = None
        budget_accounts = []
        mappings = []
        income_accounts = []
        months_ahead = 0
        operating_currencies: list[str] = []
        self.include_starting_balance = True
        self.ignore_accounts: list[re.Pattern] = []

        for e in self.entries:
            if (
                isinstance(e, Custom)
                and e.type == "fava-extension"
                and e.values[0].value == "fava_envelope"
            ):
                extension_init_date = YearMonth.of_date(e.date)
            if isinstance(e, Custom) and e.type == self.etype:
                if e.values[0].value == "start date":
                    start_date = YearMonth.of_string(e.values[1].value)
                if e.values[0].value == "budget account":
                    budget_accounts.append(re.compile(e.values[1].value))
                if e_parsed := directive.parse_directive_as(
                    e, directive.Mapping, self.etype, self.errors
                ):
                    mappings.append((e_parsed.pattern, e_parsed.account))
                if e_parsed := directive.parse_directive_as(
                    e, directive.Ignore, self.etype, self.errors
                ):
                    self.ignore_accounts.append(e_parsed.pattern)
                if e.values[0].value == "income account":
                    income_accounts.append(re.compile(e.values[1].value))
                if e.values[0].value == "currency":
                    currency = e.values[1].value
                    if not isinstance(currency, str):
                        self._append_error(
                            e.meta,
                            "Operating currency should be specified as a string.",
                        )
                        continue
                    if currency in operating_currencies:
                        self._append_error(e.meta, "Operating currency declared twice")
                        continue
                    operating_currencies.append(currency)
                if e.values[0].value == "negative rollover":
                    self._append_error(
                        e.meta,
                        "'... \"negative rollover\" STRING' directive is deprecated. Use '... \"allow negative rollowver\" BOOL'.",
                    )
                    if e.values[1].value == "allow":
                        self.negative_rollover = True
                if e.values[0].value == "allow negative rollover":
                    arg = e.values[1]
                    if arg.dtype is not bool:
                        self._append_error(
                            e.meta,
                            f"'allow negative rollover' expects bool argument, got {arg.dtype}",
                        )
                        continue
                    self.negative_rollover = arg.value
                if e.values[0].value == "months ahead":
                    months_ahead = int(e.values[1].value)
                if e.values[0].value == "include starting balance":
                    arg = e.values[1]
                    if arg.dtype is not bool:
                        self._append_error(
                            e.meta,
                            f"'include starting balance' expects bool argument, got {arg.dtype}",
                        )
                        continue
                    self.include_starting_balance = arg.value

        if len(income_accounts) == 0:
            income_accounts.append(re.compile(r"^Income:"))
        return (
            start_date if start_date else extension_init_date,
            budget_accounts,
            mappings,
            income_accounts,
            months_ahead,
            operating_currencies,
        )

    def envelope_tables(self):
        self.available_months = []
        months = self.available_months

        for month in months_between(self.start_date, self.end_date):
            months.append(month)

        self.income_df = defaultdict(Income)
        self.envelope_df = defaultdict(lambda: defaultdict(Envelope))
        self.scheduled_transactions: defaultdict[Account, defaultdict[YearMonth, list]] = defaultdict(lambda: defaultdict(list))

        self._calculate_budget_activity()
        self._calc_budget_budgeted()

        # Calculate Starting Balance Income
        if self.include_starting_balance:
            self.income_df[months[0]].avail_income += self._get_starting_balance()

        # Set envelope available funds
        for envelope_account, envelope_months in self.envelope_df.items():
            for month_index, month in enumerate(months):
                if month_index == 0:
                    prev_available = Inventory()
                else:
                    prev_available = envelope_months[months[month_index - 1]].available

                if (
                    (not self.negative_rollover)
                    and (
                        not envelope_convert.is_positive_inventory(prev_available)
                    )  # redundant but avoids converting if everything's positive.
                    and (self._reduce_inventory_to_currency(prev_available, month) < 0)
                ):
                    prev_available = Inventory()

                if envelope_months[month].allocate_fill:
                    envelope_months[month].budgeted = -(
                        prev_available + envelope_months[month].activity
                    )
                    available = Inventory()
                else:
                    available = (
                        prev_available
                        + envelope_months[month].budgeted
                        + envelope_months[month].activity
                    )
                envelope_months[
                    month
                ].available = envelope_convert.reduce_mixed_positions(
                    available,
                    self.price_map,
                    self.operating_currency,
                    month.last_day(),
                )

        for month in months:
            income = self.income_df[month]
            prev_income = self.income_df[month.prev_month()]

            income.rolled_over = prev_income.to_be_budgeted
            income.budgeted = -sum(
                [
                    account_data[month].budgeted
                    for account_data in self.envelope_df.values()
                ],
                Inventory(),
            )
            income.to_be_budgeted = envelope_convert.reduce_mixed_positions(
                income.avail_income + income.rolled_over + income.budgeted,
                self.price_map,
                self.operating_currency,
                month.last_day(),
            )

        return self.income_df, self.envelope_df, self.operating_currency

    def _reduce_inventory_to_currency(
        self, inventory: Inventory, year_month: YearMonth
    ) -> Decimal:
        pos = inventory.reduce(
            convert.convert_position,
            self.operating_currency[0],
            self.price_map,
            year_month.last_day(),
        ).get_only_position()
        return pos.units.number if pos and pos.units else Decimal(0.00)

    def _get_starting_balance(self):
        starting_balance = Inventory()
        query_str = f"select account, sum(position) from close on {self.start_date.first_day()} group by 1 order by 1;"

        cursor = beanquery.connect(
            "beancount:",
            entries=self.entries,
            errors=self.errors,
            options=self.options_map,
        ).execute(query_str)

        for row in cursor:
            row = typing.cast(tuple[Account, Inventory], row)
            account, inventory = row
            assert inventory is not None
            if any(regexp.match(account) for regexp in self.budget_accounts):
                starting_balance += inventory

        return starting_balance

    def _append_error(self, meta: Meta, message: str):
        self.errors.append(BeancountError(meta, message, None))

    def _calculate_budget_activity(self):
        # Accumulate expenses for the period
        balances: defaultdict[Account, defaultdict[YearMonth, Inventory]] = defaultdict(
            lambda: defaultdict(Inventory)
        )

        for entry in data.filter_txns(self.entries):
            # Check entry in date range
            start_date = self.start_date.first_day()
            end_date = self.end_date.last_day()
            if entry.date < start_date or entry.date > end_date:
                continue

            month = YearMonth.of_date(entry.date)

            contains_budget_accounts = False
            for posting in entry.postings:
                if any(
                    regexp.match(posting.account) for regexp in self.budget_accounts
                ):
                    contains_budget_accounts = True
                    break

            if not contains_budget_accounts:
                continue

            for posting in entry.postings:
                account = posting.account
                for regexp, target_account in self.mappings:
                    if regexp.match(account):
                        self.mappings_used[target_account].add(account)
                        account = target_account
                        break
                else:
                    self.mappings_used[account].add(account)

                # If the posting is not in a tracked currency but it includes an
                # fx conversion price, then immediately do the conversion.
                # Otherwise we will rely on the latest price at the end of the
                # month.
                if (
                    posting.units.currency not in self.operating_currency
                    and posting.price is not None
                    and posting.price.currency in self.operating_currency
                    and posting.units.number is not None
                    and posting.price.number is not None
                ):
                    converted = posting.price.number * posting.units.number
                    posting = data.Posting(
                        posting.account,
                        Amount(converted, self.operating_currency[0]),
                        posting.cost,
                        None,
                        posting.flag,
                        posting.meta,
                    )

                # Ignored accounts reduce available income (e.g., taxes, 401k contributions)
                # Instead of skipping, treat them as income deductions
                if any(regexp.match(account) for regexp in self.ignore_accounts):
                    account = "Income"  # Adds as positive, reducing net income

                elif any(regexp.match(account) for regexp in self.income_accounts):
                    account = "Income"
                elif any(
                    regexp.match(posting.account) for regexp in self.budget_accounts
                ):
                    # These accounts are in the budget. We look at the remaining
                    # accounts to know where the moeny went.
                    continue

                # Track X-flagged (scheduled/future) transactions for envelope accounts
                if entry.flag == "X" and account != "Income":
                    self.scheduled_transactions[account][month].append(posting)

                balances[account][month].add_position(posting)

        # Reduce the final balances to numbers
        sbalances: defaultdict[str, defaultdict[YearMonth, Inventory]] = defaultdict(
            lambda: defaultdict(Inventory)
        )
        for account, months in sorted(balances.items()):
            for year_month, balance in sorted(months.items()):
                # We use the last day of the month for conversion in order to
                # pick up the prices from the running month. Otherwise new
                # currencies that received a price in the middle of the month
                # will cause a crash.
                date = year_month.last_day()

                # TODO(memst): here we convert everything to one currency, look into passing the entire inventory further.
                # org_balance = balance

                total = balance.reduce(
                    envelope_convert.convert_to_operating_currency,
                    self.price_map,
                    self.operating_currency,
                    date,
                )
                # balance = balance.reduce(
                #     convert.convert_position,
                #     self.currency,
                #     self.price_map,
                #     date,
                # )

                # try:
                #     pos = balance.get_only_position()
                # except AssertionError:
                #     print(balance)

                #     import code

                #     code.interact(local=locals(), exitmsg="", banner="")
                #     raise
                # total = pos.units.number if pos and pos.units else None
                sbalances[account][year_month] = total
                # sbalances[account][year_month] = balance

        for account, monthly_activity in sbalances.items():
            for year_month, inventory in monthly_activity.items():
                # swap sign to be more human readable
                inventory = -inventory

                if account == "Income":
                    self.income_df[year_month].avail_income = inventory
                else:
                    # self.envelope_df[account][(year_month, "budgeted")] = Inventory()
                    self.envelope_df[account][year_month].activity = inventory
                    # self.envelope_df[account][(year_month, "available")] = Inventory()

    def _get_repeat_range(
        self, entry: directive._BaseEnvelopeDirective
    ) -> Iterable[YearMonth]:
        if "repeat-until" in entry.meta:
            repeat_arg = entry.meta["repeat-until"]
            try:
                repeat_until = (
                    self.end_date
                    if repeat_arg == "forever"
                    else YearMonth.of_string(repeat_arg)
                )
                return months_between(entry.month, repeat_until)
            except ValueError as e:
                self._append_error(
                    entry.meta,
                    f'repeat-until argument must be the string "forever" or in format "YYYY-MM".\n{e}',
                )
                return [entry.month]
        return [entry.month]

    def _calc_budget_budgeted(self):
        budget_lines: dict[tuple[Account, YearMonth], Meta] = {}
        allocate_fill_lines: dict[tuple[Account, YearMonth], Meta] = {}
        for e in self.entries:
            e = directive.parse_directive(e, etype=self.etype, errors=self.errors)
            if isinstance(e, directive.Allocate):
                for month in self._get_repeat_range(e):
                    if (e.account, month) in allocate_fill_lines:
                        self._append_error(
                            e.meta,
                            f"Tried to provide a budget for an envelope ({month} {e.account}) that allocates a fill.",
                        )
                        continue
                    self.envelope_df[e.account][month].budgeted.add_amount(e.amount)
                    budget_lines[e.account, month] = e.meta
            if isinstance(e, directive.AllocateFill):
                # TODO(memst): this is not quite correct because accounts that
                # had no activity will not show up in this list and the user can
                # still add a budget for it in the following lines. But
                # allocating a budget for a new account and then allocating a
                # fill would cause an error.
                accounts = [
                    account
                    for account in self.envelope_df
                    if e.account_re.match(account)
                ]
                for month in self._get_repeat_range(e):
                    for account in accounts:
                        if (account, month) in budget_lines:
                            self._append_error(
                                e.meta,
                                f"Tried to allocate a fill to an envelope ({month} {account}) with an existing budget.",
                            )
                            continue
                        self.envelope_df[account][month].allocate_fill = True
                        allocate_fill_lines[account, month] = e.meta
            if isinstance(e, directive.IncomeOverride):
                for month in self._get_repeat_range(e):
                    inv = Inventory()
                    inv.add_amount(e.amount)
                    self.income_df[month].avail_income = inv

    def get_primary_currency(self):
        return self.operating_currency[0]

    def get_price_map(self):
        return self.price_map