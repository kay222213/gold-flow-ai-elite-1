# Release verification — Gold Flow AI Elite

## Checks performed on this source bundle

- `pytest -q`: 40 passed.
- `python -m compileall -q backend validation`: passed.
- `node --check frontend/app-features.js`: passed.
- Render Blueprint parsed as YAML and required settings were inspected.
- FastAPI `TestClient` smoke checks: `/api/health` returned 200, `/api/state` returned 200, `/` served the app page, and `/api/settings` rejected an unauthenticated request with 401.
- The final ZIP passed an archive-integrity check (`unzip -t`) and contains the repository root, backend, frontend, tests, CI and deployment configuration.

## What those checks do not prove

The source bundle has not been deployed to your new Render service or tested against live paid provider credentials. This environment cannot attest to your Twelve Data, Trading Economics, FRED, OpenAI or Databento account entitlements. Those are deliberately not included in the repository. After initial deployment, enter the relevant credentials in the hosting provider's server-side environment settings. The dashboard must continue to show a provider as disconnected until its real endpoint successfully responds.

The app does not place broker orders. The Twelve Data spot quote is indicative, not an executable bid/ask. Trade-ready candidate output requires current executable pricing, verified contract metadata, fresh order-flow and depth, and a recent economic calendar. CME GC flow/depth remains a clearly labelled futures proxy for OTC spot XAUUSD.
