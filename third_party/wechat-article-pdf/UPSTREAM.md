## wechat-article-pdf vendored component

- Upstream: `https://github.com/sijiyizhan-lab/wechat-article-pdf.git`
- Upstream base commit: `e0457714c79ccecffec48c82b6a68b0d6a05c612`
- Vendored script SHA-256: `6d64672c6295374919a83fb00599a1b8bc9b08e3c12156358903ba9fa9bd0995`
- License SHA-256: `0fa72df2b1cd7b11097ccf64bf19e22595766c73f3aa19cecd96af51c07660a2`
- License: MIT; see `LICENSE` in this directory.

The vendored script includes the locally validated safety and completeness patches maintained by the same repository owner: standard-library HTTP fallback, strict source URL validation, collision-safe output reservation, local-HTML validation, image completeness metadata, CSP-safe rendering, and bounded Chrome process cleanup. The recorded hashes are verified by the macOS build script so a fresh clone does not depend on mutable files under a developer home directory.
