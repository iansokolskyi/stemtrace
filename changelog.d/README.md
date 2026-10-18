# Changelog fragments

To add a changelog entry, add a file to this directory in your pull request. Don't edit
`CHANGELOG.md`: one file per entry means pull requests don't conflict with each other.

## Add a fragment

1. Create `changelog.d/<slug>.<type>.md`. The slug is the pull request number (`152`) or a
   short name (`api-key`). The type is one of `added`, `changed`, `deprecated`, `removed`,
   `fixed` or `security`.
2. Write the entry as one or more `- ` bullets, exactly as it should appear in the
   changelog. Indent continuation lines. Don't add headings: the type sets the heading.
   `--check` parses the fragment as CommonMark. It fails on a heading at any depth, a
   link reference or footnote definition (`[label]: url`, `[^1]: note`), raw HTML, an empty bullet, and any
   top-level block other than a list that uses `-` markers. To show a `#` line or a tag,
   put it in a fenced code block or in backticks.
3. Run `uv run python scripts/assemble_changelog.py --check` to validate your fragment.

For example, `changelog.d/152.fixed.md`:

```markdown
- Workers no longer show offline while they run tasks. See [Features](https://github.com/iansokolskyi/stemtrace/blob/main/docs/features.md).
```

Write entries for users: what changed and what they need to do. Credit contributors
(`thanks @user, #152`). A change users don't notice, such as a test or CI change, doesn't
need a fragment.

## Preview the result

To print the changelog as the release would assemble it, run:

```bash
uv run python scripts/assemble_changelog.py --dry-run
```

## How fragments are released

When a maintainer runs `make release-pr`, `scripts/assemble_changelog.py` merges the
fragments into `## [Unreleased]` in `CHANGELOG.md` and deletes them. It puts each entry under
the `### <Type>` heading in Keep a Changelog order and sorts entries by file name within a
type. Text above the first `###` heading in Unreleased, such as a **Highlights:** block,
stays on top.

CI runs `--check`, which fails on a fragment with an invalid name or content. While
`changelog.d/` has fragments, `--check` and `make release-pr` apply the same rule to the
text under each `###` heading in Unreleased, because the merge would file that text under
the heading.
