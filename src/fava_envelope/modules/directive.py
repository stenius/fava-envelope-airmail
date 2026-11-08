import re
from typing import Any, TypeVar, overload

import beancount.core.account
from beancount.core.amount import Amount
from beancount.core.data import Account, Custom, Meta
from beancount.parser.grammar import ValueType
from fava.helpers import BeancountError

from fava_envelope.modules.year_month import YearMonth


class DirectiveValidationError(BaseException):
    """Helper class for creating and raising a BeancountError."""

    error: BeancountError

    def __init__(self, meta: Meta, message: str):
        self.error = BeancountError(meta, message, None)


class _BaseEnvelopeDirective:
    month: YearMonth
    meta: Meta

    def __init__(self, directive: Custom):
        self.month = YearMonth.of_date(directive.date)
        self.meta = directive.meta

    @classmethod
    def _directive_str(cls):
        """Convert a class to a string that it expects in the custom directive.

        Replaces 'CamelCase' class name to 'space case'.
        """
        return re.sub(r"(?<!^)(?=[A-Z])", " ", cls.__qualname__).lower()

# These TypeVars can be removed in 3.12
TA = TypeVar("TA")
TB = TypeVar("TB")
TC = TypeVar("TC")
@overload
def _cast_values(
    values: list[ValueType], classes: tuple[type[TA]], meta
) -> tuple[TA]: ...
@overload
def _cast_values(
    values: list[ValueType], classes: tuple[type[TA], type[TB]], meta
) -> tuple[TA, TB]: ...
@overload
def _cast_values(
    values: list[ValueType], classes: tuple[type[TA], type[TB], type[TC]], meta
) -> tuple[TA, TB, TC]: ...


def _cast_values(values: list[ValueType], classes: tuple, meta: Meta):
    """Given values, ignore the first element and verify that the rest is consistent with classes argument.

    Raises DirectiveValidationError otherwise.
    """
    if len(values) != len(classes) + 1:
        raise DirectiveValidationError(
            meta,
            f"Invalid number of arguments: Expected {len(classes)}, got {len(values)-1}.",
        )
    new_values = []
    for i, (arg, cls) in enumerate(zip(values[1:], classes)):
        value, dtype = arg
        if cls is re.Pattern and dtype is str:
            value = re.compile(value)
            dtype = re.Pattern

        # Not `is not`: beancount.core.account.TYPE is a string, and it comes back
        # from beancount's pickle cache equal but not identical.
        if dtype != cls:
            raise DirectiveValidationError(
                meta,
                f"expected argument {i+1} ({value}) to be of type {cls}, got {dtype}",
            )
        new_values.append(value)
    return tuple(new_values)


class Allocate(_BaseEnvelopeDirective):
    account: Account
    amount: Amount

    def __init__(self, directive: Custom):
        super().__init__(directive)
        self.account, self.amount = _cast_values(
            directive.values, (beancount.core.account.TYPE, Amount), directive.meta
        )


class AllocateFill(_BaseEnvelopeDirective):
    account_re: re.Pattern

    def __init__(self, directive: Custom):
        super().__init__(directive)
        (self.account_re,) = _cast_values(
            directive.values, (re.Pattern,), directive.meta
        )


class IncomeOverride(_BaseEnvelopeDirective):
    amount: Amount

    def __init__(self, directive: Custom):
        super().__init__(directive)
        (self.amount,) = _cast_values(directive.values, (Amount,), directive.meta)


class Mapping(_BaseEnvelopeDirective):
    pattern: re.Pattern
    account: Account

    def __init__(self, directive: Custom):
        super().__init__(directive)
        self.pattern, self.account = _cast_values(
            directive.values,
            (
                re.Pattern,
                beancount.core.account.TYPE,
            ),
            directive.meta,
        )


class Ignore(_BaseEnvelopeDirective):
    """Directive to ignore certain accounts from envelope budgeting."""
    pattern: re.Pattern

    def __init__(self, directive: Custom):
        super().__init__(directive)
        (self.pattern,) = _cast_values(
            directive.values, (re.Pattern,), directive.meta
        )


budget_directive_types: list[type[_BaseEnvelopeDirective]] = [
    Allocate,
    AllocateFill,
    IncomeOverride,
]


def parse_directive(
    entry: Any,
    etype: str,
    errors: list[BeancountError],
    directive_types: list[type[_BaseEnvelopeDirective]] = budget_directive_types,
) -> None | _BaseEnvelopeDirective:
    if not isinstance(entry, Custom):
        return None
    if entry.type != etype:
        return None
    if len(entry.values) == 0:
        return None
    entry_name = entry.values[0].value
    for cls in directive_types:
        if cls._directive_str() == entry_name:
            try:
                return cls(entry)
            except DirectiveValidationError as e:
                errors.append(e.error)
                return None

    return None


T = TypeVar("T", bound=_BaseEnvelopeDirective)
def parse_directive_as(
    entry: Any, type: type[T], etype: str, errors: list[BeancountError]
) -> T | None:
    return parse_directive(entry, etype, errors, directive_types=[type])  # type: ignore
