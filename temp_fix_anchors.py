import re

with open('d:/tradline/streamlit_executive_dashboard/executive_pulse/ui.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Remove the render_section_anchor calls from within the section renderers
# These are now handled by app.py
lines = text.split('\n')
new_lines = []
for line in lines:
    # Skip lines that call render_section_anchor inside section functions
    stripped = line.strip()
    if stripped.startswith('render_section_anchor(') and stripped.endswith(')'):
        # Check it's NOT the function definition
        continue
    new_lines.append(line)

text = '\n'.join(new_lines)

with open('d:/tradline/streamlit_executive_dashboard/executive_pulse/ui.py', 'w', encoding='utf-8') as f:
    f.write(text)

print("Removed duplicate section anchors from ui.py")
