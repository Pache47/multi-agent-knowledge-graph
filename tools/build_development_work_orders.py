"""Rebuild the fictional PDF source; contains no extraction or answer expectations."""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/shared_folder/work_orders_october.pdf"


def build():
    styles = getSampleStyleSheet()
    styles["Normal"].leading = 16
    styles["Normal"].spaceAfter = 12
    styles["Title"].alignment = 0
    styles["Heading2"].textColor = colors.HexColor("#344b5b")
    story = []

    def text(value, style="Normal"):
        story.append(Paragraph(value, styles[style]))

    text("Maintenance job closeout", "Title")
    text("WO-604 | Filling area | 15 Oct 2026")
    text("Equipment: feed pump (P21). Setup tag: H2.")
    text("Call from production", "Heading2")
    text("Transfer to the filler slowed during batch 61. Operator heard rattling near the inlet.")
    text("Fitter entry", "Heading2")
    text("Product residue found on the inlet strainer. No impeller inspection recorded.")
    text("Cleaned the strainer and refitted the cover seal. No replacement part booked.")
    text("Handover / closure", "Heading2")
    text("Flow recovered during a ten-minute water run. Product run deferred until next shift.")
    text("Assigned: R. Lim | Time booked: 35 min | Follow-up: product trial outstanding")
    story.append(Spacer(1, 18))
    text("Exported 18 Oct 2026. Attachment 1 of 2.")
    story.append(PageBreak())
    text("Fitter's continuation sheet", "Title")
    text("16 October 2026 - late shift - WO-611")
    text("Called to the rinse pump. Equipment tag field left empty.")
    text("Wet patch at the return pump coupling after rinse cycle. Drip continued after wipe-down.")
    text("Possibly the cover seal; source of the leak not confirmed.")
    text("Work done: none entered. Stores request sent for identification of the seal kit.")
    text("Outcome: blank. Day shift to inspect. Configuration field: blank.")
    story.append(Spacer(1, 18))
    text("Unrelated stores note: two empty chemical drums awaiting collection.")
    text("Exported 18 Oct 2026. Attachment 2 of 2.")

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(48, 28, "SIMULATED DATA | Fictional maintenance record")
        canvas.drawRightString(A4[0] - 48, 28, f"Page {doc.page}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        leftMargin=48,
        rightMargin=48,
        topMargin=48,
        bottomMargin=48,
        title="October maintenance attachments",
        author="Fictional plant maintenance",
        invariant=1,
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    print(OUTPUT.relative_to(ROOT))


if __name__ == "__main__":
    build()
