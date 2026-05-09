---
# Optional. If omitted, the page title is taken from the first H1, then
# the file stem.
title: KEX i CTFYS — kort guide

# Authors are shown in the rendered footer ("Sammanställt av …" /
# "Compiled by …"). Two accepted shapes:
#
#   1) a plain string (no role)
#   2) a {name, role} mapping
#
# You can mix them in the same list.
authors:
  - studievägledarna på SCI-skolan
  - name: Christian Ohm
    role: Programansvarig CTFYS

# Free-form date string; rendered verbatim ("senast uppdaterad …" /
# "last updated …"). ISO-8601 (YYYY-MM-DD) is the recommended convention.
updated: 2026-05-09
---

# KEX i CTFYS — kort guide

Detta är ett exempel på en kuraterad markdown-fil som boten kan citera.
Filen ska ligga under `docs/corpus/markdown/` (eller någon annan
underkatalog som **inte** är `web_import/`) för att rendreras via
`/doc/<rel_source>` med rubrik, brödtext och författarfält.

## Varför kuratera?

Webbsidor på `kth.se` täcker programmets struktur men inte alltid hur
processer fungerar i praktiken. Kuraterade `.md`-filer är ett ställe där
du kan formulera svar tydligare än de officiella källorna gör — och
fortfarande peka studenten till en rendering som visar vem som står
bakom texten.

## Vad kan jag skriva?

Vanliga markdown-element fungerar:

- Rubriker (`##`, `###`)
- Punktlistor och numrerade listor
- **Fet** och *kursiv* text
- Länkar: [KTH:s programsida](https://www.kth.se/student/kurser/program/CTFYS)
- Kodblock med inline `kod` eller fenced blocks
- Tabeller

| Kurs | Hp | Status |
|------|----|--------|
| SA114X | 15 | KEX (CTFYS) |
| EF112X | 15 | KEX (CTFYS) |

## Vad ska jag undvika?

- Rå HTML — renderaren kör i `html: false`-läge, så `<script>` etc.
  passeras igenom som text. Det ska aldrig finnas behov av rå HTML.
- Att förlita dig på exakta sidnummer eller paragrafer — dessa hör
  hemma i de officiella PDF:erna, inte i den kuraterade texten.
