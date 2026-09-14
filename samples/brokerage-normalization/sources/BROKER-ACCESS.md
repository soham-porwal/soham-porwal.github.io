# Broker source access and evidence boundaries

Research date: 2026-09-10 America/Los_Angeles. Acquisition UTC is recorded in
external-provenance.json (2026-09-11 UTC). No authenticated broker request or live
customer account response was made or observed. Research used bounded public
official-page checks followed by five external-fixture searches.

## Fidelity

Official [Trader+ Positions guide](https://www.fidelity.com/trading/trader-desktop-user-guide/positions)
confirms CSV export, selectable position filters, and customizable columns.
It does not publish a literal frozen CSV header/sample in the accessed text.

Official [Open Positions help](https://www.fidelity.com/webcontent/ap002390-mlo-content/20.01/help/learn_open_positions.shtml)
documents display fields, downloaded displayed information, account/security
filters, pending basis updates, and differing pricing freshness. These are UI
semantics, not verified current literal CSV headers. Its legacy-looking URL
also limits confidence in applying every UI detail to the current exporter.
Grouped/filtered output does not establish complete account coverage. Do not
assume one refresh time means identical quote times for every holding.

### Acquired external test sample

- `fidelity-external-source.mjs.txt`: exact upstream JavaScript source bytes.
- `fidelity-external-2026-07-02.csv`: safely extracted Fidelity literal content,
  preserving source BOM, CRLF, date, fields, values, blank row, and disclaimer.
- `fidelity-LICENSE.txt`: exact upstream MIT license, copyright 2026 Kameron Kales.
- `external-provenance.json`: immutable source URLs, hashes, timestamp and extraction.

Source: [pinned author fixture](https://github.com/holdequity/planfi-import/blob/591fbf7aadf2a3e30245868a8f034da8d154fd73/fixtures/csv-sandbox.mjs).
Evidence class: **external_synthetic_fixture**. The as-of July 2, 2026 is an
author-chosen test date, not an authenticated broker report date. No fetched
JavaScript was executed. Source includes fictitious accounts and test holdings.
It is useful for testing an explicitly named dialect, not proof of an official
contract. Anomaly: the source comment says no Type column, while the literal has
Type. Preserve the literal and flag the discrepancy.

An [August 29, 2026 forum report](https://www.bogleheads.org/forum/viewtopic.php?p=8845129)
found in search excerpts claims Fidelity headers changed casing (for example,
Account number versus Account Number). This is anecdotal first-person evidence,
not an independently authenticated native file. Record it as a dialect-drift lead.

## Schwab / TD Ameritrade

Accessed [Schwab developer portal](https://developer.schwab.com/) and
[Trader API specification](https://developer.schwab.com/products/trader-api--individual/details/specifications/Retail%20Trader%20API%20Production).
Both returned zero extracted text. This establishes an extraction gap; it does
not prove authentication is required or the API unavailable. No official account
schema or live positions response was verified. Do not promote unofficial SDK
reconstructions into verified official contracts, or silently equate TD legacy
and current Schwab schema versions.

Additional public inspection of [schwab-rs fixtures](https://github.com/major/schwab-rs/tree/e79d1a8d79fdf7f531817d4ee3700ce2926ca0ed/tests/fixtures)
found quote/streaming examples, not a native positions report. Account tests
construct typed objects. No verified native 2026 account report was found within
the search budget; that is not a claim that none exists.

## Robinhood

[API docs](https://docs.robinhood.com/) redirected to crypto/trading. Indexed
official crypto docs show paginated holdings with string quantity fields, but
these do not establish equities semantics. Never treat crypto holdings as an
equities contract.

Material 2026 correction: the indexed official [May 27 announcement](https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/)
announces Agentic Trading beta with equities and dedicated accounts/MCP servers.
This was read as a search excerpt, not the full page. The opened
[Agentic support page](https://robinhood.com/us/en/support/agentic-trading/)
links overview/trading instructions but yielded no holdings JSON schema. Thus
"Robinhood has only a crypto API" is not supported. Equities contract verification
remains incomplete in this bounded check.

An external [golden fixture test](https://github.com/zaydiscold/robinhood-cli-mcp-api/blob/d0f09cb760f171bd94bb6e1b604504aa5a63683f/cli/test/portfolio-pnl.test.ts)
was inspected read-only. It constructs fictitious accounts 111/222 and equities
with symbol, instrument_id, and string quantity; it has no native snapshot date.
Its surrounding 2026 release notes do not make it a 2026 customer report. Do not
inherit its unconditional option multiplier assumptions. It was not acquired or
licensed for redistribution in this scope.

## Supported boundary

Unknown source versions, incomplete acquisition, ambiguous currency/identity,
adjusted option deliverables, unsupported asset valuation, missing required
basis, and grouped/pending rows must remain explicit errors/quarantines or
typed non-position records. Never invent successful native broker support from
a documentation gap or externally constructed test object.
