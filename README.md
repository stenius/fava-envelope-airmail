# fava-envelope-airmail

A fork of [memst/fava-envelope](https://github.com/memst/fava-envelope) (itself a fork of [polarmutex/fava-envelope](https://github.com/polarmutex/fava-envelope)) that adds several enhancements for envelope budgeting in [Fava](https://github.com/beancount/fava)/[Beancount](https://github.com/beancount/beancount).

I'm open to merging these changes upstream, but the original repos haven't seen much merge activity lately, so I decided to publish this fork separately in case anyone else finds the enhancements useful.

## What's New in This Fork

### Budget Statistics Page
A new **Stats** view accessible from the budget page header that provides:
- Overall summary cards: total periods tracked, total/average income, total budgeted vs. spent, and overall budget adherence rate
- Per-envelope statistics: period counts, under/over budget breakdowns, adherence percentages, average activity, and standard deviation
- Categorization of envelopes into "Well-Managed" (>80% adherence) and "Needs Attention" (<50% adherence)
- Color-coded stats table rows by adherence rate

### Scheduled Transaction Tracking
- Transactions flagged with `X` (e.g., upcoming/scheduled bills) are now tracked per envelope
- A badge appears next to envelope names showing the count and total of scheduled transactions for that month (e.g., `2X`)

### Account Ignore Directive
A new `ignore` directive to exclude accounts from envelope budgeting (e.g., taxes, 401k contributions). Ignored accounts are treated as income deductions rather than being completely skipped:
```
2020-01-01 custom "envelope" "ignore" "Expenses:Taxes:.*"
```

### Color-Coded Budget Table
- Envelope rows are color-coded based on account type and available balance:
  - **Expenses**: green (under budget), yellow (exactly zero), red (overspent)
  - **Assets**: blue (negative/receivable), green (positive)
  - **Liabilities**: treated like expenses
- Income "To be budgeted" row is color-coded: green (surplus), red (deficit), yellow (zero)
- Individual amounts within rows show positive/negative coloring
- Full dark mode support

### Other Improvements
- Envelopes are now sorted alphabetically
- Default month view is the current month (instead of the last available month)
- Redundant "Amount" column is hidden when only one currency is configured
- Rolled-over allowance column shows positive/negative coloring

## Inherited Features (from memst/fava-envelope)

- No pandas dependency (faster)
- Cached envelope results until source files change
- Multi-currency support
- Simplified budgeting table
- Proper `ACCOUNT` types in directive parsing
- Advanced directives: `allocate fill`, `income override`, `allow negative rollover`, `include starting balance`, `repeat-until` metadata

Please check the [changelog](https://github.com/stenius/fava-envelope-airmail/blob/master/CHANGELOG.md) for full details.

## Installation

### From PyPI

```bash
pip install fava-envelope-airmail
```

### From Source

```bash
git clone https://github.com/stenius/fava-envelope-airmail.git
cd fava-envelope-airmail
pip install -e .
```

## Usage

### Load the Extension
Add this to your beancount journal, and start fava as normal:
```
2000-01-01 custom "fava-extension" "fava_envelope" "{}"
```

You should now see 'Envelope' in your fava sidebar. You must set up a budget (see below), or Fava will report a 404 error.

### Setting Up a Budget

#### Start date
```
2020-01-01 custom "envelope" "start date" "2020-01"
```

#### Budget months ahead
Show future months for budgeting ahead (default is 0):
```
2020-01-01 custom "envelope" "months ahead" "2"
```

#### Budget accounts
Specify the Assets and Liabilities included in your budget (supports regex):
```
2020-01-01 custom "envelope" "budget account" "Assets:Checking"
2020-01-01 custom "envelope" "budget account" "Liabilities:Credit-Cards:.*"
```

#### Mappings
Map sub-accounts to a parent envelope:
```
2020-01-01 custom "envelope" "mapping" "Expenses:Food:.*" Expenses:Food
```

#### Ignore accounts
Exclude accounts from budgeting (e.g., taxes deducted from paychecks):
```
2020-01-01 custom "envelope" "ignore" "Expenses:Taxes:.*"
```

#### Allocate money to an envelope
```
2020-01-31 custom "envelope" "allocate" Expenses:Food 100.00 USD
```

#### Operating currency
The extension reads the operating currency from beancount options (defaults to USD):
```
option "operating_currency" "EUR"
```

## License

MIT
