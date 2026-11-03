# Changelog

All notable changes are listed here. The project follows
[Semantic Versioning](https://semver.org/); before 1.0, minor versions may
include breaking changes, which are called out.

## Unreleased

- A link in the question is read first and treated as the main source: it is
  cited as [1], read on every depth, and a question about the page alone is
  answered without a search.
- The citation check rules on every cited statement of an answer: there is no
  ceiling of three calls any more, the judge's reply has room for every verdict
  in its batch, and a batch whose reply fails is halved and asked again instead
  of ending the check.
- Fewer false "partly" verdicts: the judge reads a claim's cited sources
  together and counts a paraphrase as support. Every claim ruled against gets
  a second look that splits it into its statements and asks for the source's
  words for each; the verdict follows from which quotes are found on the page,
  and the note names the statements that are not.
- The citation check is an icon under the answer that shows its verdict:
  orange with the number of problems, light green when every statement held,
  grey when it did not run. While it runs, the eye animates in the icon.

## 0.1.0

First public release.
