# Discover task

Input: project configuration and discovery prompt. Output an array of `Candidate` objects conforming to `../../schemas/Candidate.schema.json`. Score each candidate using the configured weights, retain reasons, and preserve pending/rejected outcomes for review. In fixture mode, read the current fixture round rather than browse.
