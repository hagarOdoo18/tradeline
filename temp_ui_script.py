import re

with open('d:/tradline/streamlit_executive_dashboard/executive_pulse/ui.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Modify render_global_filters signature and return
text = re.sub(
    r'def render_global_filters\((.*?)\) -> tuple\[FilterState, str\]:',
    r'def render_global_filters(\1) -> FilterState:',
    text
)

# Remove the call to render_navigation inside render_global_filters
text = re.sub(
    r'\s+segment = render_navigation\(\)',
    r'',
    text
)

# Remove the render_navigation definition
text = re.sub(
    r'def render_navigation\(\) -> str:.*?return section',
    r'',
    text,
    flags=re.DOTALL
)

# In render_global_filters, update return
text = re.sub(
    r'    return state, segment',
    r'    return state',
    text
)


old_nav = '''<div class="bottom-nav-dock">
  <a href="#snapshot-section">dY"S Snapshot</a>
  <a href="#finance-section">dY' Finance</a>
  <a href="#sales-section">dY _ Sales</a>
  <a href="#inventory-section">dY" Inventory</a>
  <a href="#pipeline-section">dYZ_ Pipeline</a>
</div>'''

new_nav = '''<div class="bottom-nav-dock">
  <a href="#snapshot-section" onclick="event.preventDefault(); document.getElementById('snapshot-section').scrollIntoView({behavior: 'smooth'});">&#128202; Snapshot</a>
  <a href="#finance-section" onclick="event.preventDefault(); document.getElementById('finance-section').scrollIntoView({behavior: 'smooth'});">&#128181; Finance</a>
  <a href="#sales-section" onclick="event.preventDefault(); document.getElementById('sales-section').scrollIntoView({behavior: 'smooth'});">&#128722; Sales</a>
  <a href="#inventory-section" onclick="event.preventDefault(); document.getElementById('inventory-section').scrollIntoView({behavior: 'smooth'});">&#128230; Inventory</a>
  <a href="#pipeline-section" onclick="event.preventDefault(); document.getElementById('pipeline-section').scrollIntoView({behavior: 'smooth'});">&#128640; Pipeline</a>
</div>'''

if old_nav in text:
    text = text.replace(old_nav, new_nav)
else:
    # Just generic replace if encoding messed up the emojis
    text = re.sub(
        r'<div class="bottom-nav-dock">.*?</div>',
        new_nav,
        text,
        flags=re.DOTALL
    )

with open('d:/tradline/streamlit_executive_dashboard/executive_pulse/ui.py', 'w', encoding='utf-8') as f:
    f.write(text)
print('UI modified successfully')
