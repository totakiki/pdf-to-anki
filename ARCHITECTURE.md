# v5.3 Architecture

```text
PDF page
  ↓
Text + Vision analysis
  ├ atomic facts
  ├ visual_relations
  ├ image_regions
  └ fact-specific evidence_bbox
  ↓
QA generation
  ↓
Quality enforcement
  ├ compound-card detection
  ├ visual_dependency check
  ├ text-only correction
  └ fact-specific image binding
  ↓
Image Occlusion (optional)
  ↓
SQLite / editor / Anki export
```

v5.2のrequest pacing、429 retry、503 backoff、fallback model、checkpoint/resumeは維持しています。
