# Compare and report task

Input: baseline and current `ProductSnapshot` collections. Emit confirmed `ChangeEvent` instances using `../../schemas/ChangeEvent.schema.json`, then a `Digest` using `../../schemas/Digest.schema.json`. Prioritize price, availability, and material feature changes. A delivery adapter receives only the final digest.
