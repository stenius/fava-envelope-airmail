from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import statistics

from beancount.core import prices
from beancount.core.data import Account
from beancount.core.inventory import Inventory
from fava.context import g
from fava.ext import FavaExtensionBase

from fava_envelope.modules import convert
from fava_envelope.modules.beancount_envelope import BeancountEnvelope
from fava_envelope.modules.budgets import Envelope, Income
from fava_envelope.modules.envelope_link import EnvelopeLink
from fava_envelope.modules.year_month import YearMonth, YearMonthStr


@dataclass
class EnvelopeStats:
    """Statistics for a single envelope across all periods up to current date."""
    envelope_name: str
    periods_under_budget: int = 0
    periods_over_budget: int = 0
    periods_at_budget: int = 0
    total_periods: int = 0
    total_budgeted: Decimal = Decimal(0)
    total_activity: Decimal = Decimal(0)
    avg_activity: Decimal = Decimal(0)
    std_dev_activity: Decimal = Decimal(0)
    avg_budgeted: Decimal = Decimal(0)
    avg_available: Decimal = Decimal(0)
    budget_adherence_rate: Decimal = Decimal(0)  # % of periods at or under budget
    activity_values: list[Decimal] = field(default_factory=list)


@dataclass
class OverallStats:
    """Overall budget statistics across all envelopes."""
    total_periods: int = 0
    total_income: Decimal = Decimal(0)
    avg_income_per_month: Decimal = Decimal(0)
    total_budgeted: Decimal = Decimal(0)
    total_spent: Decimal = Decimal(0)
    overall_adherence_rate: Decimal = Decimal(0)
    envelopes_usually_under: list[str] = field(default_factory=list)  # >80% under budget
    envelopes_often_over: list[str] = field(default_factory=list)  # >50% over budget


class EnvelopeBudget(FavaExtensionBase):
    """The class handling the display of budgets in Fava."""

    # Stores whether the up-to-date version of the budget repors has been
    # cached.
    ran_budget_df: bool

    income_tables: defaultdict[YearMonth, Income]
    envelope_tables: defaultdict[Account, defaultdict[YearMonth, Envelope]]
    primary_currency: str
    price_map: prices.PriceMap

    available_months: list[YearMonth]
    available_months_str: list[YearMonthStr]

    report_title = "Envelope Budget"

    def after_delete_entry(self, entry):
        self.reset_cache()

    def after_entry_modified(self, entry, new_lines):
        self.reset_cache()

    def after_insert_entry(self, entry):
        self.reset_cache()

    def after_insert_metadata(self, entry, key, value):
        self.reset_cache()

    def after_load_file(self):
        self.reset_cache()

    def after_write_source(self, path, source):
        self.reset_cache()

    def reset_cache(self):
        self.ran_budget_df = False

    def generate_budget_df(self, currency):
        if self.ran_budget_df:
            return

        module = BeancountEnvelope(
            g.ledger.all_entries,
            self.ledger.options,
            currency,
            g.ledger.errors,
        )
        # TODO memst: I don't like how this code is structured and BeancountEnvelope
        # should have a better interface. The evnvelope_tables is computed only once per
        # instance anyway (argumentless function), so it should be completely fine to make
        # the whole thing a dataclass or something similar where you can just access some
        # values after creation.
        (self.income_tables, self.envelope_tables, _) = module.envelope_tables()
        self.envelope_to_accounts = module.mappings_used
        self.scheduled_transactions = module.scheduled_transactions
        self.primary_currency = module.get_primary_currency()
        self.price_map = module.price_map

        self.available_months = module.available_months
        self.available_months_str = [str(month) for month in self.available_months]

        self.ran_budget_df = True

    def get_budgets_months_available(self, currency):
        self.generate_budget_df(currency)
        return self.available_months_str

    def check_month_in_available_months(self, month, currency):
        return bool(month and month in self.get_budgets_months_available(currency))

    def get_currencies(self):
        if "currencies" in self.config:
            return self.config["currencies"]
        return None

    def get_current_month(self):
        """Return the current month as a string in YYYY-MM format."""
        today = date.today()
        return f"{today.year}-{today.month:02d}"

    def generate_income_query_tables(self, month_str: str):
        primary_currency_amt_string = f"Amount ({self.primary_currency})"
        income_table_types = []
        income_table_types.append(("Name", str(str)))
        income_table_types.append((primary_currency_amt_string, str(Inventory)))
        # Only show raw Amount column if multiple currencies are configured
        currencies = self.get_currencies()
        show_raw_amount = currencies and len(currencies) > 1
        if show_raw_amount:
            income_table_types.append(("Amount", str(Inventory)))

        income_table_rows = []

        if month_str is not None:
            month = YearMonth.of_string(month_str)
            for name, amount in [
                ("Income this month", self.income_tables[month].avail_income),
                ("Funds from last month", self.income_tables[month].rolled_over),
                ("Budgeted for month", self.income_tables[month].budgeted),
                ("To be budgeted for month", self.income_tables[month].to_be_budgeted),
            ]:
                row = {
                    "Name": name,
                    primary_currency_amt_string: convert.convert_inventory_to_operating_currency(
                        amount,
                        self.price_map,
                        self.primary_currency,
                        month.last_day(),
                    ),
                }
                if show_raw_amount:
                    row["Amount"] = amount
                income_table_rows.append(row)

        return income_table_types, income_table_rows

    def generate_envelope_query_tables(self, month_str: str):
        envelope_table_types = []
        envelope_table_types.append(("Envelope", str(EnvelopeLink)))
        envelope_table_types.append(("Rolled-over Allowance", str(Inventory)))
        envelope_table_types.append(("Budgeted", str(Inventory)))
        envelope_table_types.append(("Activity", str(Inventory)))
        envelope_table_types.append(("Available", str(Inventory)))

        envelope_table_rows = []

        if month_str is not None:
            month = YearMonth.of_string(month_str)
            for envelope_name, e_row in sorted(self.envelope_tables.items()):
                if e_row[month].is_empty():
                    continue
                row = {}
                # Calculate scheduled transaction count and total
                scheduled_postings = self.scheduled_transactions.get(envelope_name, {}).get(month, [])
                scheduled_count = len(scheduled_postings)
                scheduled_total = ""
                if scheduled_count > 0:
                    total_inv = Inventory()
                    for posting in scheduled_postings:
                        total_inv.add_position(posting)
                    # Convert to primary currency for display
                    total_inv = convert.convert_inventory_to_operating_currency(
                        total_inv, self.price_map, self.primary_currency, month.last_day()
                    )
                    scheduled_total = str(-total_inv)  # Negate to show as positive expense
                row["Envelope"] = EnvelopeLink(envelope_name, month_str, self.envelope_to_accounts[envelope_name], scheduled_count, scheduled_total)
                row["Rolled-over Allowance"] = e_row[month.prev_month()].available
                row["Budgeted"] = e_row[month].budgeted
                row["Activity"] = e_row[month].activity
                row["Available"] = e_row[month].available
                envelope_table_rows.append(row)

        return envelope_table_types, envelope_table_rows
    
    @staticmethod
    def extra_escape(url: str):
        """
        
        Flask's url_for method does not escape some characters that Chrome (and mabe
        other browsers) try to replace, which causes unnecessary url navigations. Add
        extra escapes on top of that function. """

        # Have to ensure that the first ? character that separates the tags from the rest
        # of the url is not escaped.
        spl = url.split('?', maxsplit=1)
        if len(spl) == 1:
            return url
        
        escaped = (spl[1]
                .replace(':', r'%3A')
                .replace('$', r'%24')
                .replace("(", r"%28")
                .replace(")", r"%29")
                .replace("?", r"%3F")
                )

        return spl[0] + "?" + escaped

    def _get_inventory_amount(self, inventory: Inventory, month: YearMonth) -> Decimal:
        """Extract decimal amount from inventory, converting to primary currency."""
        converted = convert.convert_inventory_to_operating_currency(
            inventory, self.price_map, self.primary_currency, month.last_day()
        )
        total = Decimal(0)
        for pos in converted:
            if pos.units.number is not None:
                total += pos.units.number
        return total

    def _get_months_up_to_current(self) -> list[YearMonth]:
        """Get list of months from start up to and including current month."""
        today = date.today()
        current_month = YearMonth(today.year, today.month)
        return [m for m in self.available_months if m <= current_month]

    def generate_envelope_stats(self, currency: str) -> list[EnvelopeStats]:
        """Generate statistics for each envelope up to current date."""
        self.generate_budget_df(currency)

        months = self._get_months_up_to_current()
        if not months:
            return []

        stats_list = []

        for envelope_name, envelope_months in sorted(self.envelope_tables.items()):
            stats = EnvelopeStats(envelope_name=envelope_name)
            activity_values = []
            available_values = []
            budgeted_values = []

            for month in months:
                envelope = envelope_months[month]

                # Skip months with no activity and no budget
                if envelope.is_empty():
                    continue

                stats.total_periods += 1

                # Get amounts in primary currency
                activity_amt = self._get_inventory_amount(envelope.activity, month)
                budgeted_amt = self._get_inventory_amount(envelope.budgeted, month)
                available_amt = self._get_inventory_amount(envelope.available, month)

                stats.total_activity += activity_amt
                stats.total_budgeted += budgeted_amt

                activity_values.append(activity_amt)
                available_values.append(available_amt)
                if budgeted_amt != 0:
                    budgeted_values.append(budgeted_amt)

                # For expense envelopes (negative activity = spending)
                # Under budget means: activity (negative) + budgeted (positive) >= 0
                # i.e., you spent less than budgeted
                if envelope_name.startswith("Expenses:"):
                    # activity is negative for spending, budgeted is positive
                    remaining = budgeted_amt + activity_amt
                    if remaining > 0:
                        stats.periods_under_budget += 1
                    elif remaining < 0:
                        stats.periods_over_budget += 1
                    else:
                        stats.periods_at_budget += 1
                else:
                    # For non-expense envelopes, just check if available is positive
                    if available_amt >= 0:
                        stats.periods_under_budget += 1
                    else:
                        stats.periods_over_budget += 1

            # Calculate averages and std dev
            if stats.total_periods > 0:
                stats.avg_activity = stats.total_activity / stats.total_periods
                stats.avg_available = sum(available_values) / len(available_values) if available_values else Decimal(0)
                stats.avg_budgeted = sum(budgeted_values) / len(budgeted_values) if budgeted_values else Decimal(0)

                # Budget adherence = (under + at) / total
                adherent_periods = stats.periods_under_budget + stats.periods_at_budget
                stats.budget_adherence_rate = (Decimal(adherent_periods) / Decimal(stats.total_periods)) * 100

                # Standard deviation
                if len(activity_values) > 1:
                    # Convert to float for statistics module, then back to Decimal
                    float_values = [float(v) for v in activity_values]
                    stats.std_dev_activity = Decimal(str(statistics.stdev(float_values)))

                stats.activity_values = activity_values

            # Only include envelopes with actual data
            if stats.total_periods > 0:
                stats_list.append(stats)

        return stats_list

    def generate_overall_stats(self, currency: str) -> OverallStats:
        """Generate overall budget statistics."""
        self.generate_budget_df(currency)

        months = self._get_months_up_to_current()
        if not months:
            return OverallStats()

        overall = OverallStats()
        overall.total_periods = len(months)

        # Calculate income totals
        for month in months:
            income = self.income_tables[month]
            income_amt = self._get_inventory_amount(income.avail_income, month)
            overall.total_income += income_amt

        if overall.total_periods > 0:
            overall.avg_income_per_month = overall.total_income / overall.total_periods

        # Get envelope stats for aggregation
        envelope_stats = self.generate_envelope_stats(currency)

        total_adherent = 0
        total_periods_all = 0

        for es in envelope_stats:
            overall.total_budgeted += es.total_budgeted
            overall.total_spent += abs(es.total_activity)  # Activity is negative for expenses

            total_adherent += es.periods_under_budget + es.periods_at_budget
            total_periods_all += es.total_periods

            # Categorize envelopes
            if es.total_periods >= 3:  # Need at least 3 periods for meaningful stats
                if es.budget_adherence_rate >= 80:
                    overall.envelopes_usually_under.append(es.envelope_name)
                elif es.budget_adherence_rate < 50:
                    overall.envelopes_often_over.append(es.envelope_name)

        if total_periods_all > 0:
            overall.overall_adherence_rate = (Decimal(total_adherent) / Decimal(total_periods_all)) * 100

        return overall

    def generate_stats_table(self, currency: str):
        """Generate table data for the stats page."""
        envelope_stats = self.generate_envelope_stats(currency)

        table_types = [
            ("Envelope", str(str)),
            ("Periods", str(int)),
            ("Under Budget", str(int)),
            ("Over Budget", str(int)),
            ("Adherence %", str(str)),
            (f"Avg Activity ({self.primary_currency})", str(str)),
            (f"Std Dev ({self.primary_currency})", str(str)),
            (f"Total Budgeted ({self.primary_currency})", str(str)),
            (f"Total Activity ({self.primary_currency})", str(str)),
        ]

        table_rows = []
        for es in envelope_stats:
            row = {
                "Envelope": es.envelope_name,
                "Periods": es.total_periods,
                "Under Budget": es.periods_under_budget,
                "Over Budget": es.periods_over_budget,
                "Adherence %": f"{es.budget_adherence_rate:.1f}%",
                f"Avg Activity ({self.primary_currency})": f"{es.avg_activity:,.2f}",
                f"Std Dev ({self.primary_currency})": f"{es.std_dev_activity:,.2f}",
                f"Total Budgeted ({self.primary_currency})": f"{es.total_budgeted:,.2f}",
                f"Total Activity ({self.primary_currency})": f"{es.total_activity:,.2f}",
            }
            table_rows.append(row)

        return table_types, table_rows

