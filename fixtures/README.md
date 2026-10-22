# NEXUS deterministic demo fixtures

**Everything in this folder is fictional demo data.** It exists so NEXUS can run repeatable,
offline missions with the deterministic fake provider. Missions created from these fixtures are
stored in projects flagged `is_demo` and are labeled "DEMO / offline fake" in the UI.

- `demo-research/` - fictional sources about the fictional town of Larkspur. One source
  (`regional-news.md`) is a syndicated copy of the survey and declares the same
  `source-lineage`, so citing both does not count as two independent sources.
- `demo-code/` - a tiny Python package plus `.nexus-demo/solutions.json`, the scripted
  "answers" the fake engineer writes. Two tasks deliberately edit the same file region so
  integration produces a real git conflict.
- `injection/` - a document containing a prompt-injection attempt, used by security tests.
