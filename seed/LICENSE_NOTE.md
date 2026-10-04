# Seed document license

**File:** `elements_of_style.pdf`
**Source:** *The Elements of Style* by William Strunk Jr. (1920), Project Gutenberg eBook #37134
(<https://www.gutenberg.org/ebooks/37134>).

**Why it's freely redistributable:** first published in 1920, so its US copyright has expired and
it is in the public domain. Project Gutenberg's own terms additionally grant a license to copy,
distribute, and reuse its digitized edition, which is what this PDF is built from.

**How it was built:** Project Gutenberg only publishes this title as HTML/EPUB/plain text, not PDF.
`scripts/build_seed_pdf.py` paginates the plain-text UTF-8 edition
(`https://www.gutenberg.org/cache/epub/37134/pg37134.txt`) into a readable PDF with `reportlab`
(a one-off build dependency, not part of the app's own `requirements.txt`). The full Project
Gutenberg license text from the source file is preserved on the final pages, per its terms for
redistributing the work.

Run it yourself with: `pip install reportlab && python scripts/build_seed_pdf.py`
