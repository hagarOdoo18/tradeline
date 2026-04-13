import re

with open('d:/tradline/streamlit_executive_dashboard/executive_pulse/theme.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Remove Tabs CSS
text = re.sub(r'/\*\s*──\s*tabs\s*\(glass\s*pills\)\s*──\s*\*/.*?/\*\s*──\s*topbar\s*──\s*\*/', r'/* ── topbar ── */', text, flags=re.DOTALL)

# 2. Update bottom-nav-dock to prevent hiding on mobile and improve styling
text = re.sub(
    r'\.bottom-nav-dock\s*\{(.*?)\}',
    r'''.bottom-nav-dock {
                position: fixed;
                bottom: 24px;
                left: 50%;
                transform: translateX(-50%);
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 8px;
                background: rgba(26, 26, 26, 0.85);
                backdrop-filter: blur(16px) saturate(180%);
                -webkit-backdrop-filter: blur(16px) saturate(180%);
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 9999px;
                box-shadow: 0 20px 40px -8px rgba(0, 0, 0, 0.3), 0 0 0 1px rgba(255,255,255,0.05);
                z-index: 10000;
                max-width: 96vw;
                overflow-x: auto;
            }''',
    text,
    count=1,
    flags=re.DOTALL
)

# Fix bottom-nav-dock anchors to look good on dark background
text = re.sub(
    r'\.bottom-nav-dock a\s*\{(.*?)\}',
    r'''.bottom-nav-dock a {
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 10px 18px;
                color: rgba(255, 255, 255, 0.85);
                text-decoration: none;
                font-size: 13px;
                font-weight: 700;
                border-radius: 99px;
                transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
                white-space: nowrap;
            }''',
    text,
    count=1,
    flags=re.DOTALL
)

text = re.sub(
    r'\.bottom-nav-dock a:hover\s*\{(.*?)\}',
    r'''.bottom-nav-dock a:hover {
                color: var(--tl-green-bright);
                background: rgba(255, 255, 255, 0.1);
            }''',
    text,
    count=1,
    flags=re.DOTALL
)

# 3. Add responsive breakpoints
# Replace the media queries section
new_media = '''/* ── responsive ── */
            @media (max-width: 1180px) {
                .hero-grid { grid-template-columns: 1fr; }
                .hero-copy h1 { max-width: none; font-size: 2.5rem; }
            }
            @media (max-width: 900px) {
                .block-container { padding: 0 10px 100px !important; }
                .premium-card {
                    flex: 0 0 210px;
                    min-width: 210px;
                    max-width: 245px;
                    min-height: 170px;
                    padding: 10px 12px;
                }
            }
            @media (max-width: 768px) {
                /* On tablet devices, shrink the text */
                .metric-value { font-size: 1.5rem; }
                div[data-testid="stVerticalBlockBorderWrapper"]:has(.filter-start) {
                    padding: 0.6rem 1rem !important;
                }
                .bottom-nav-dock a { padding: 8px 12px; font-size: 12px; }
            }
            @media (max-width: 600px) {
                .bottom-nav-dock { bottom: 16px; gap: 4px; padding: 6px; }
                /* Show icons only on mobile */
                .bottom-nav-dock a { font-size: 0; padding: 10px; }
                .bottom-nav-dock a::first-letter { font-size: 18px; }
                
                /* Stack cards vertically instead of horizontal scroll */
                .card-horizontal-container {
                    flex-direction: column !important;
                    align-items: stretch !important;
                    padding: 8px !important;
                    gap: 12px !important;
                    overflow: visible !important;
                }
                .premium-card { max-width: 100%; min-width: 100%; min-height: auto; }
            }
            @media (max-width: 480px) {
                .section-title-row { flex-direction: column; align-items: start; gap: 4px; }
                .section-title-row h2 { font-size: 1.5rem; }
            }'''
            
text = re.sub(
    r'/\*\s*──\s*responsive\s*──\s*\*/.*',
    new_media + '\n            </style>\n            """\n        ).strip(),\n        unsafe_allow_html=True,\n    )\n',
    text,
    flags=re.DOTALL
)

with open('d:/tradline/streamlit_executive_dashboard/executive_pulse/theme.py', 'w', encoding='utf-8') as f:
    f.write(text)

print("CSS transformed")
