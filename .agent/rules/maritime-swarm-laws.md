# Maritime Swarm Laws (Sniper Stack, Feb 2026)

1. Mission lock only: complete maritime applications end-to-end with proof.
2. Deterministic execution: same input profile produces same decisions.
3. Zero babysit: no manual prompts unless hard external blocker.
4. Form-first strategy: fill, validate, submit, verify confirmation.
5. ATS-aware navigation: detect redirects and continue in-platform.
6. No dead loops: skip impossible paths quickly and log reason.
7. Resume attach priority: always upload resume when file input exists.
8. Pitch fidelity: use profile pitch exactly, no mutation.
9. Sea-days fidelity: use profile sea-days note exactly, no mutation.
10. EEO policy binding: enforce eeo-policy.md every run.
11. Asset blocking: block images, video/media, fonts, and stylesheet payloads.
12. DOM JS injection first: use JavaScript DOM injection for fill/click speed.
13. Parallelism cap: execute in batch size 3 parallel workers max.
14. Fail-fast TTL: hard timeout 90 seconds per target workflow.
15. Self-heal loop: on bug/stuck, parse logs, apply hotfix hints, retry up to 15 attempts.
16. Acceptance law: COMPLETE only when confirmation text, success URL, or success proof screenshot exists.
17. Immutable audit: write every attempt state to targets.json and git history.
18. Trunk policy: push to main only after green acceptance.
