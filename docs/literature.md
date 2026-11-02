# Literature and PDFs

Simplixity has two ways of working with scientific papers: the **Literature**
page, for finding and screening papers, and **literature projects**, for asking
questions that are answered only from the papers you have. Separately, any
chat can take attached files.

## Building a reading list

**Tools → Literature** opens a page of its own (`/literature`). It follows the
steps of a systematic search done by hand.

1. **Talk.** Describe what you need sources for. The model asks a question or
   two and writes a brief: the research question, inclusion and exclusion
   criteria, a year range, and English search queries. The brief is shown and
   can be edited; everything after it is measured against it.
2. **Search.** The queries go to IEEE Xplore (with an API key), OpenAlex,
   Semantic Scholar, Crossref and arXiv directly, not through SearXNG, which
   cuts abstracts and has no stable ids. Copies of the same paper are merged,
   including a preprint and its published version, and a page of 25 is kept.
3. **Screen.** Each abstract is summarised against the brief, with what sets
   the paper apart and a suggested verdict. A paper without an abstract is not
   summarised and says so. You decide with `1` relevant, `2` maybe, `3`
   exclude; `j`/`k` move, `a` opens the abstract, `u` undoes. A reason for an
   exclusion steers the next round.
4. **Search again.** Your decisions now steer the search: new queries built
   from the vocabulary of the relevant papers, Semantic Scholar
   recommendations from your picks and rejections, the citation graph around
   your picks, and a ranking moved towards the picks and away from the
   rejections (Rocchio feedback on embeddings, keyword overlap without them).
   No paper is shown twice, and each way of finding papers gets some places on
   the page.
5. **Export** the relevant papers, optionally with the maybes, as BibTeX.
   Entries come from doi.org where a DOI exists and are completed from the
   screened record where the publisher deposited less. Keys look like
   `vaswani2017attention`, and every entry carries a `url`, so the file
   doubles as a download list.

Every round keeps a report: the queries, what each database returned, and
every database that rate-limited or refused. A thin round caused by a refusing
database is visible as such.

The page has its own model picker in the top bar. That model handles the
conversation, query planning and screening, and also writes and checks answers
in literature projects. Left alone it is the chat model. Screening means
judging abstracts against criteria, and small local models do it noticeably
worse.

Excluded papers keep their rows. They are what stops a later round from
showing them again, and what "unlike these" is computed from.

## Full texts

The app does not download papers, because publishers want your own login.
Download the PDFs yourself and drop them on the reading list's **Full texts**
tab. Each file is matched to its paper by the DOI or arXiv id on its first
pages, by the file name, or by the full title. Anything uncertain waits for
you to assign it, and an assignment whose title is not found in the PDF is
flagged. The tab shows what is ready, what is missing (with a link), and how
far each PDF is indexed. A scan without a text layer is reported, not silently
indexed as empty.

A paper that no search round found can be added with **Add your own papers**,
or with **New paper** on a file that is waiting. It becomes a relevant paper,
described by OpenAlex when the record's title appears on the PDF's first pages,
and otherwise from the PDF itself.

PDFs are stored in SQLite with their text page by page. The text is cut into
passages that never cross a page boundary, and each passage is embedded with
the embedding model from the settings. When that model changes, passages are
re-embedded rather than mixed. Two-column pages that were extracted row by row
are re-read column by column, and bibliography pages are never searched.

## Literature projects

A project that contains a reading list is a literature project. Its chats are
answered from the papers marked relevant there: their full texts, and for a
relevant paper without a PDF, its abstract, labelled as such.

The composer shows a **Local / Web** switch in such a project instead of depth
and source controls. Local is the default for every question; Web adds the web
for the question you ask with it on. The server enforces the same rule, so a
chat filed in a literature project is bound to its papers unless the request
turns the web on. Earlier chats in the project are only searched when the
**Chats** switch is on.

What keeps these answers checkable:

- **A citation names the passage.** The writer cites `[4: "the exact words on
  that page"]`. The answer shows the paper number and page (`1, p. 33`);
  hovering shows the words, and a click opens the PDF on that page with the
  passage highlighted. A bare `[4]` refers to the page as a whole. Page
  numbers are PDF pages, not the journal's printed numbers.
- **The search can come back empty.** Keywords must cover a minimum share of a
  query's terms, and embeddings only count when they clearly single out some
  passages. A question your papers do not cover gets no answer, said as such,
  rather than one written from the model's own knowledge.
- **The model's own reasoning is marked.** Explanations, applications to your
  example and recommendations go in a blockquote, and the check lists them as
  own reasoning rather than as missing sources.
- **Every sentence is checked**, not only cited ones. A sentence without a
  citation is checked against the next cited source in its paragraph, and a
  sentence tied to no source is listed as such.
- **Quotations are compared word for word** with the cited page, without a
  model. One that does not appear verbatim cannot pass as supported.

None of this makes an answer true. It makes every sentence traceable to a page
you can open, and says plainly which ones are not.

Copying an answer writes each citation out in full, with the paper's byline
and page in parentheses. "p." or "S." follows the language of the answer.

## Files attached to a chat

Any chat takes PDF, Word (`.docx`), Markdown and text files, through the
paperclip, drag and drop, or paste. They go through the same pipeline as
uploaded papers: pages, passages, embeddings, page citations and the reader.

Word, Markdown and text files are cut into parts of roughly a page and cited as
`(part N)` rather than by page, since Word's page numbers depend on whoever
opens the file. Short attachments are handed to the writer whole for any
question, and a request such as "summarise this" reads the file from start to
end instead of searching it.

A chat with attachments is bound to them in the same way as a literature
project (with the Web switch to add the web), but keeps the chat model.
Deleting the chat deletes its files.
