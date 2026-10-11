# Interactive Brokers (IBKR)

cgt-calc reads the CSV downloaded from the **Transaction History** page in the Interactive Brokers
Client Portal. It does not read Activity Statements, Flex Queries, PDFs or Excel files.

Use an account whose base currency is GBP. The importer checks the base currency in the CSV and
rejects other currencies. Include taxable accounts only; activity inside an ISA is outside the
capital gains calculation.

## Export the transaction history

Export the complete history rather than only the tax year. Earlier acquisitions can still affect
disposals in the year being calculated, and the cash deposits at the start of the account help the
balance check detect an incomplete export.

1. Sign in to the Interactive Brokers Client Portal in a web browser.
2. Open **Performance & Reports → Transaction History**. Alternatively, open **Menu → Reporting →
    Transaction History**.
3. Select **Custom** as the period and set the start to the account's first transaction. Set the end
    to today, or at least 30 days after the end of the tax year you are calculating.
4. Clear any transaction-type or symbol filters so the export includes trades, income, fees and cash
    movements.
5. Download the table using its CSV export control.

IBKR's current
[Transaction History instructions](https://www.ibkrguides.com/clientportal/transaction-history.htm)
show the menu path, period selector, column configuration and filters.

Keep the downloaded CSV unchanged. The filename does not matter.

Do not substitute an Activity Statement or a Flex Query CSV: their sections and headings are
different even though they are also available from **Performance & Reports**.

You can compare the file's structure with the
[synthetic example CSV](https://github.com/cgt-calc/capital-gains-calculator/blob/main/tests/interactive_brokers/data/test_basic.csv).

## Generate the report

For the 2025/26 tax year, run:

```shell
cgt-calc --year 2025 --interactive-brokers-file U12345678.TRANSACTIONS.20240101.20260505.csv
```

`--year 2025` means 6 April 2025 to 5 April 2026. Follow [Generate and Review a Report](../usage.md)
to find and check the output.

The Transaction History export contains cash deposits and withdrawals, so a complete export should
normally pass the balance check. Do not add `--no-balance-check` simply to bypass an error; first
check the date range and filters as described in [Troubleshooting](#troubleshooting).

## Supported activity

The importer recognises these literal values from the CSV's `Transaction Type` column:

| Transaction type              | How cgt-calc handles it                                            |
| ----------------------------- | ------------------------------------------------------------------ |
| `Buy`, `Sell`                 | Acquisitions and disposals of shares, funds and options; see below |
| `Transaction Fee`             | A cost of that day's trades in the security it names               |
| `Investment Interest Paid`    | Accrued interest bought with a bond; balance only                  |
| `Dividend`, `Payment in Lieu` | Dividend income                                                    |
| `Foreign Tax Withholding`     | Tax deducted at source; treated as dividend tax                    |
| `Credit Interest`             | Interest income                                                    |
| `Debit Interest`              | Interest charged on a borrowed balance; balance only               |
| `Deposit`, `Withdrawal`       | Cash movements used by the balance check                           |
| `Other Fee`                   | A charge such as an ADR fee or market data; balance only           |
| `Sales Tax`                   | VAT on an account-level service such as market data; balance only  |
| `Adjustment`                  | Cash-balance adjustments such as `FX Translations P&L`             |
| `Forex Trade Component`       | The base-currency net of a currency conversion; balance only       |

For a GBP-base account, IBKR reports gross amounts, commissions and net amounts in GBP, so every row
is recorded in GBP whatever the security is priced in. `Price Currency` describes the unit price
alone: when the CSV also supplies an `Exchange Rate`, cgt-calc converts a foreign-currency unit
price to GBP before calculating the acquisition or disposal.

cgt-calc does not treat a currency conversion as a disposal. It applies the `Forex Trade Component`
Net Amount to the cash balance and reports no gain or loss on the conversion itself.

An `Other Fee` only changes the tracked cash balance, whether it names a security, as an ADR fee
does, or none, as a market-data subscription does. cgt-calc does not add it to the allowable cost of
any holding. An ADR fee is a charge for holding the shares and a subscription a charge for running
the account, and HMRC treats a fee as an
[incidental cost](https://www.gov.uk/hmrc-internal-manuals/capital-gains-manual/cg15250) you can
deduct only where it was paid for a purchase or a sale.

cgt-calc does not work out what an `Other Fee` was paid for. IBKR's
[reporting reference](https://www.ibkrguides.com/reportingreference/reportguide/statement%20of%20fundsfq.htm)
lists a corporate action election fee among its other fees too, and a fee like that may have been
paid for a purchase or a sale. If one of yours was, the gain cgt-calc reports on those shares is too
high by that fee. To have it deducted, delete the `Other Fee` row from a working copy of the export
and add its amount to both the `Commission` and the `Net Amount` of the trade it was paid for: a
-5.00 fee on a sale turns a `Commission` of -3.00 into -8.00 and a `Net Amount` of 597.00 into
592.00.

IBKR descriptions for dividends and payments in lieu can put an ISIN in parentheses immediately
after the symbol. cgt-calc uses that identifier when deciding whether a supported double-taxation
treaty applies.

### Costs of a trade

A trade's allowable costs are the whole gap between its `Gross Amount` and `Net Amount`: the
commission and, on a UK share, the stamp duty and PTM levy, which IBKR adds to `Net Amount` without
showing them in `Commission`
([incidental costs](https://www.gov.uk/hmrc-internal-manuals/capital-gains-manual/cg15250)). A
`Net Amount` better than `Gross Amount` plus `Commission` is refused, because the difference is not
a cost.

A `Transaction Fee` naming a security, such as Spain's financial transaction tax, is a cost of that
day's trades in it. Its rows for the day, including any cancellation and recharge, are added up and
shared between those trades by quantity. One with no trade that day is refused. A `Transaction Fee`
naming no security is an account charge and only moves cash.

### Options

Exchange-traded US equity and index options, named by their OCC symbol such as
`ABC   250620P00010000`, are supported. The export does not say whether a trade opened or closed a
position, so cgt-calc reads each series in date order and works it out from the running position. A
trade that takes a position through zero is split into the part that closes it and the part that
opens the other side.

- **Bought options** are held like shares, one holding per series, named as IBKR describes it, such
    as `ABC 20JUN25 10 C`, and matched under the same-day, 30-day and Section 104 rules. One that
    expires without being exercised is a disposal for nothing on its expiry date, so its cost is a
    loss ([TCGA 1992 s144(4)](https://www.legislation.gov.uk/ukpga/1992/12/section/144)).
    Cash-settled index options such as `SPX` are treated the same way.
- **Written options** are disposals on the day they are written, with the premium as the proceeds
    ([CG55536](https://www.gov.uk/hmrc-internal-manuals/capital-gains-manual/cg55536)). Buying one
    back is an allowable cost of that grant, oldest grant first, and the gain stays dated on the day
    the option was written
    ([CG55545](https://www.gov.uk/hmrc-internal-manuals/capital-gains-manual/cg55545)). Re-run the
    earlier tax year when an option written in it is bought back in a later one. An option that
    expires leaves the premium as the gain.

The export has no row for an expiry, so cgt-calc infers one for a position still open after its
expiry date, but only when the export runs at least seven days past it and its rows add up to the
change in cash its summary states. Otherwise, a settlement could be missing, and it stops instead.
Exercise and assignment are not supported: if a trade in the underlying could be an option's
delivery, a multiple of 100 shares at the strike between the option's first trade and a week after
its expiry, cgt-calc stops.

### Bonds

A UK gilt, which IBKR names `UKT` followed by its coupon and maturity, such as `UKT 4 1/4 12/07/27`,
is exempt from capital gains tax
([TCGA 1992 s115](https://www.legislation.gov.uk/ukpga/1992/12/section/115)), so its trades only
move cash. Any other bond is refused: whether it is a qualifying corporate bond cannot be told from
the row. Coupons and accrued interest are income, which the accrued income scheme can affect; check
them against your own records.

### Rows with no symbol

IBKR writes `-` in the `Symbol` column on a row that names no security. cgt-calc reads that as no
symbol, rather than as a holding called `-`, so one of the types above is handled differently
depending on whether a security is named. A `Foreign Tax Withholding` naming a security is dividend
tax. One naming no symbol is tax withheld from your cash interest, so it is reported as interest
tax.

### Tickers and exchange listings

IBKR can report one security under different tickers. cgt-calc combines supported ticker pairs into
one holding when it can identify the security by ISIN, from your file or from the bundled list of
securities, and reports it under one ticker, which may not be the one you traded: `CSPX`, for
example, is reported as `CSP1`. An ISIN in a dividend description can also identify trades under
that ticker, wherever the dividend appears in your input.

Without an ISIN or reference mapping, tickers can remain separate without an error. If your report
shows one security twice, compare the ISINs in its dividend descriptions or IBKR's instrument
details. If they match, add all its verified tickers to one row in your
[ISIN to ticker mapping](../extra-data-and-options.md#isin-to-ticker-translation). Rerun and check
that the report shows one holding.

Adding a mapping does not add support for a new ticker pair. If the holding remains split, report
both tickers and the ISIN as described in [Troubleshooting](#troubleshooting); do not rely on that
holding's calculated gain until resolved. If your input assigns different ISINs to one ticker,
cgt-calc stops with an error.

## Known limitations

- Only the transaction types listed above are mapped. Any other value stops the import with
    `Unknown type`; do not delete the row merely to make the calculation run.
- Cash `Deposit` and `Withdrawal` rows are supported, but transfers of shares or funds between
    accounts are not. Corporate actions such as splits, mergers and spin-offs are not mapped from an
    IBKR export either; record a split or consolidation as a RAW
    [`STOCK_SPLIT` row](raw.md#share-reorganisations) stating the change to your whole pooled
    holding. If the export contains the corporate-action row, it stops the import, so remove it from
    a working copy only when adding the RAW replacement. See
    [Corporate actions from a Flex Query](#corporate-actions-from-a-flex-query) to get these rows
    from IBKR rather than write them by hand.
- The importer does not read an asset-class field. It supports shares, funds, the options and the
    gilts described above; do not rely on it to calculate futures, contracts for difference or
    crypto assets.
- The Transaction History page exports a limited period at once, and cgt-calc reads one file. See
    [Several exports of one account](#several-exports-of-one-account).
- The `Account` column is not used to keep separate ledgers. Rows for multiple taxable IBKR accounts
    in one CSV are combined under one broker balance and portfolio.

## Tools for IBKR data

The repository's `scripts/cgt_tools` command prepares IBKR data for cgt-calc. Run it from a clone
with `uv run python -m scripts.cgt_tools --help`.

### Several exports of one account

`ibkr-merge` joins Transaction History exports into the one file cgt-calc reads:

```shell
uv run python -m scripts.cgt_tools ibkr-merge merged.csv export-2025.csv export-2026.csv
```

Where exports overlap, the most recently generated one supplies the rows, and the merge stops if
they disagree. Each export must add up to the change in cash its summary states, so a filtered one
is refused. IBKR ends each export with an `FX Translations P&L` row revaluing foreign cash over that
export's period; these are replaced by one row that makes the merged file add up, and move cash
only. `--drop DATE,TYPE,SYMBOL` moves a row out, for a trade that has to go in a RAW file with a
corporate action on the same day.

### Corporate actions from a Flex Query

A Flex Query has the splits and spin-offs the Transaction History lacks. In the Client Portal,
create an Activity Flex Query with the **Corporate Actions** section and all its fields, in CSV
format, covering the account's whole history. Download it, or enable the **Flex Web Service** for a
token and fetch it:

```shell
export IBKR_FLEX_TOKEN=... IBKR_FLEX_QUERY=...
uv run python -m scripts.cgt_tools ibkr-fetch flex.csv
uv run python -m scripts.cgt_tools ibkr-corporate-actions flex.csv corporate_actions.csv spin_offs.csv
```

Pass the two files to cgt-calc with `--raw-file` and `--spin-offs-file`. Splits and consolidations
become `STOCK_SPLIT` rows and spin-offs become `SPIN_OFF` rows, with the company each came from. Any
other event, such as a merger, stops the command: each needs its own treatment. Check each date
against the company's announcement, because a spin-off is dated the first day both shares trade.

### Checking a report

`cross-check` recalculates every disposal with separate, simpler code and compares it with
cgt-calc's report, written with `--no-pdflatex`. It covers the same-day, 30-day and Section 104
rules across Trading 212, IBKR and RAW files, splits, spin-offs at a given share of cost, and
options. Run `cross-check --help` for its options.

## Troubleshooting

### `Unexpected base currency`

The CSV says that the account's base currency is not GBP. Changing the text in the file would not
convert any amounts, so use a GBP-base account export instead.

### `Price is in ... but the Exchange Rate column is missing or empty`

A `Buy` or `Sell` row prices the trade in a currency other than GBP but gives no rate to convert it
with, while its gross amount, commission and net amount are in GBP. The price cannot be checked
against the amount, and a guessed rate would put a wrong cost or proceeds in the report, so the
import stops. Re-export the statement with the `Exchange Rate` column filled in for that row — a
`Buy` or `Sell` always needs a price, so clearing `Price` and `Price Currency` only trades this
error for a missing-price one. Other row types are unaffected: only a trade's price feeds the
calculation, so a dividend, fee or interest row keeps a foreign, unconverted `Price Currency` rather
than being refused over a price nothing reads.

### `Couldn't find Transaction History header`

Check that the file came from **Performance & Reports → Transaction History** and is the CSV export.
Activity Statements, Flex Queries and files converted from PDF use different layouts.

### `CSV header mismatch`

The Transaction History page lets you configure its columns, while cgt-calc requires the fields it
uses and rejects fields it does not recognise. Export the standard table without changing its
columns. `Price Currency` and `Exchange Rate` are accepted optional columns.

If an unchanged export still fails, first upgrade cgt-calc using the same method you used to install
it. If the error remains, open a
[GitHub issue](https://github.com/cgt-calc/capital-gains-calculator/issues/new) with:

- your cgt-calc version from `cgt-calc --version`;
- the complete error message; and
- the CSV header and a sanitised failing row.

Do not upload an unredacted file: the report contains account identifiers, holdings and other
sensitive financial information.

### `Unknown type`

Compare the named transaction type with [Supported activity](#supported-activity). The row may
represent an unsupported corporate action or position transfer. Do not remove it unless you replace
it with an equivalent supported transaction whose UK tax treatment you have verified.

### `Reached a negative balance`

Re-export the history from the account's first transaction and make sure no transaction-type or
symbol filter is active. A shortened or filtered export can omit the deposit, sale or dividend that
funded a later transaction.

Use `--no-balance-check` only if you have confirmed why the export cannot reconcile and have checked
its completeness another way.
