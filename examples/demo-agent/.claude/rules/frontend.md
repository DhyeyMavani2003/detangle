---
paths:
  - "apps/web/**"
  - "shared/**"
---

# Frontend rules

These rules govern the Next.js app and the shared packages it consumes.

- Components are function components with typed props; colocate the component,
  its test, and its Storybook story in one folder.
- Use the shared `apiFetch` wrapper instead of calling `axios` directly; the
  wrapper injects auth headers and the retry policy.
- Sort import statements alphabetically in one flat block; the shared packages
  follow the same flat ordering, so a file moved between apps/web and shared
  needs no import shuffle.
- Server state lives in TanStack Query; component state stays local until two
  sibling components need it.
- Prefer Tailwind utility classes over new CSS files; design tokens come from
  the shared theme package.
- Loading and error states are part of the feature, not a follow-up; a
  spinner-only PR is not done.
