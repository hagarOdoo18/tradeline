# Tradeline Intelligence design lock

Primary concept: `static/description/design/product-360-concept.png` (1536 × 1024).

## Visible-copy lock

- Tradeline Intelligence
- Product 360, Customer 360, Bundle Lab, Audience Builder, Launch Cockpit, Data Quality
- Export insight
- What customers buy with this product, who they are, and what to do next.
- Baskets, Companion attach rate, Baskets with companions, Identified-customer coverage
- Frequently bought together, Customer & payment mix, Data coverage
- Next best action, Audience opportunity, Build audience
- Recommended launch action, Open Launch Cockpit

Dynamic product names, dates, measures, customer names, source labels and recommendations are allowed and must come from the service response.

## Design system

- Background lock: true white analysis canvas, near-black `#070a08` shell, never cream.
- Accent: Tradeline green `#00713b`; positive/data green `#18a957`; pale data track `#dcecdf`.
- Typography: Inter/system sans; 44 px product heading; 30 px KPI values; 13–16 px deliberate UI chrome.
- Container model: open rails, tables and bands; the black insight rail is the only major framed surface.
- Dividers: 1 px neutral gray; radii limited to inputs, buttons and selected rows.
- Icons: consistent 1.8 px outline SVGs, white in navigation and green on selected state.
- Motion: 160 ms selected/hover transitions and a subtle loading skeleton; respect reduced motion.
- Responsive: desktop three-column shell; tablet collapses insight rail below analysis; mobile nav becomes a horizontal rail and tables scroll.

## Core interaction path

Search product → select suggestion → set dates/source → inspect ranked companions → select a companion → preview identified audience → open customer, bundle, audience, launch and coverage views → export recomputed CSV.
