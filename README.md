# PMQs exchanges, 1961 to the present

Every question-and-answer exchange at Prime Minister's Questions in the UK House of Commons since 1961, built from the official record (Hansard) and updated automatically after each Wednesday's session.

The dataset underpins a research project on the tone of PMQs, including whether new Prime Ministers' honeymoons are getting shorter. The current release is **v0.9.0** (pre-release): text and metadata only. Coded measures of hostility and combativeness are planned for v1.0.

## What's here

| Path | Contents |
|---|---|
| `data/pmqs/<year>.csv` | One row per exchange: an MP's question and the Prime Minister's reply |
| `data/diagnostics/<year>.csv` | Every sitting day checked, and why any were excluded |
| `data/reference/` | How pre-1997 questioners were identified, and names still unresolved |
| `scripts/` | The code that builds the dataset |
| `.github/workflows/` | The automated weekly update and the name-resolution run |

## Columns

| Column | Meaning |
|---|---|
| `exchange_id` | Date plus position in the session, e.g. `2025-03-12-007` |
| `date` | Sitting date |
| `pm` | Prime Minister answering |
| `sequence` | Position of the exchange within the session |
| `q_name` | Questioner's name as printed in Hansard |
| `q_pid` | ParlParse person ID, where identified |
| `q_party` | Questioner's party |
| `q_role` | `government`, `opposition`, `leader_of_opposition`, `collective` (e.g. "Hon. Members") or `unknown` |
| `is_formula` | 1 if the question is only the "official engagements" formula |
| `q_words`, `a_words` | Word counts of question and answer |
| `question`, `answer` | Full text |
| `q_match` | Pre-1997 files only: how the questioner was identified |

## Things to know before using it

- **Format change in 1997.** Until May 1997 PMQs ran twice a week (Tuesday and Thursday, 15 minutes each); since then, once a week on Wednesdays for 30 minutes. Analyses spanning the change should aggregate by week, not by session.
- **Parties before 1997** are not in the source data. They were reconstructed by matching names to MPs and assigning the party each was elected under, with mid-term party changes applied from the House of Commons Library and Wikipedia lists of MPs who changed party. Role follows party membership, not whip status: MPs who temporarily lost or resigned the whip but stayed in their party (such as the 1994–95 Maastricht rebels) keep their party's role. About 1.4% of pre-1997 questioners remain unidentified (2.5% in the 1960s, about 1% thereafter).
- **Stand-in sessions**, where a minister answered in the Prime Minister's absence, are excluded and logged in the diagnostics.
- **Older text** comes from digitised Hansard and contains some scanning errors.
- **The 2010–15 coalition**: Liberal Democrats are coded as government.

## Sources and attribution

- House of Commons Hansard: Parliamentary copyright, re-used under the [Open Parliament Licence](https://www.parliament.uk/site-information/copyright-parliament/open-parliament-licence/).
- Daily debates and member data: [ParlParse / TheyWorkForYou](https://parser.theyworkforyou.com/), mySociety.
- General election results: [UK Parliament, uk-general-elections](https://github.com/ukparliament/uk-general-elections).
- Speaker identification for ambiguous pre-1997 names: [Historic Hansard](https://api.parliament.uk/historic-hansard/).

## Licence and citation

The data is released under CC BY 4.0 (see `data/LICENSE.md`); the code under the MIT licence (see `LICENSE`). To cite, use the "Cite this repository" button on GitHub or the Zenodo DOI for the version you used.
