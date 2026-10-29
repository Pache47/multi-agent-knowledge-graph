"""Build fictional source PDFs. Requires reportlab; run from any directory."""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "shared_folder" / "manuals"
OUT.mkdir(parents=True, exist_ok=True)
styles = getSampleStyleSheet()
styles.add(ParagraphStyle("Label", fontName="Helvetica-Bold", fontSize=9,
                         textColor=colors.HexColor("#8d3a21"), spaceAfter=12))
styles.add(ParagraphStyle("Copy", fontName="Helvetica", fontSize=10.5,
                         leading=15, spaceAfter=9, alignment=TA_LEFT))
styles["Title"].alignment = TA_LEFT
styles["Title"].fontSize = 23
styles["Title"].leading = 27
styles["Heading2"].textColor = colors.HexColor("#164e63")


def footer(canvas, doc):
    canvas.setStrokeColor(colors.HexColor("#cbd5e1"))
    canvas.line(20 * mm, 18 * mm, 190 * mm, 18 * mm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#475569"))
    canvas.drawString(20 * mm, 13 * mm, "FICTIONAL TRAINING DATA | Not manufacturer guidance")
    canvas.drawRightString(190 * mm, 13 * mm, f"Page {doc.page}")


def build(filename, title, metadata, sections):
    story = [Paragraph("SIMULATED MANUAL - INTERVIEW DEMONSTRATION", styles["Label"]),
             Paragraph(title, styles["Title"]), Paragraph(metadata, styles["Copy"]),
             Spacer(1, 4 * mm)]
    for heading, body in sections:
        story.extend([Paragraph(heading, styles["Heading2"]), Paragraph(body, styles["Copy"])])
    doc = SimpleDocTemplate(str(OUT / filename), pagesize=(210 * mm, 297 * mm),
                            leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=19 * mm, bottomMargin=25 * mm,
                            title=title, author="Fictional Packaging Equipment")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


build("CT-200_configuration_B_rev3.pdf", "CT-200 conveyor<br/>Configuration B",
      "Document CT200-B | Revision 3 | Issued 1 September 2026", [
    ("1. Applicability", "Applies only to CT-200 configuration B. Confirm the installed configuration in the equipment record. A newer publication date alone does not establish applicability. This fictional conveyor uses a motor, gearbox, drive, belt, support rollers and package sensors."),
    ("2. Motor overload", "The simulated drive label MOTOR_OVERLOAD means motor protection detected excessive loading over time. The label does not identify a unique failed component. Possible explanations include mechanical resistance or obstruction, motor-related problems, or incorrect motor configuration. A recent product change is context, not proof of cause."),
    ("3. Inspection boundary", "Use the site-approved maintenance procedure referenced as SITE-M01. Physical checks require authorised staff and the site's isolation and verification process. Review existing observations, including where noise was heard. Binding rollers are one possible source of resistance. If the required detailed procedure is unavailable, consult the maintenance lead. This summary does not supply repair or restart instructions."),
    ("4. Replacement reference", "For the underside return roller assembly on configuration B, consult the approved parts list: RA-200B, assembly including bearings. RA-200A is not an approved substitute. Stock and reservations must be confirmed separately with stores."),
    ("5. Other stops", "A conveyor can also stop because a downstream machine is not ready. A production readiness signal is distinct from a safety stop and from a drive-reported motor overload. The records must identify which condition occurred.")
])

build("CT-200_configuration_A_rev1.pdf", "CT-200 conveyor<br/>Configuration A",
      "Document CT200-A | Revision 1 | Issued 1 November 2025 | Archived copy", [
    ("1. Applicability", "Applies only to CT-200 configuration A. This copy is retained for equipment still using configuration A and for historical maintenance records. It does not cover machines upgraded to configuration B. Check the installed equipment record before using a parts reference."),
    ("2. Motor overload", "An overload report is a reason to investigate, not confirmation of motor failure. Mechanical resistance and motor-related problems can have similar reported symptoms. Follow the applicable site maintenance process; this fictional extract supplies no electrical settings or detailed repair steps."),
    ("3. Roller assembly", "Configuration A uses underside return roller assembly RA-200A, including bearings. This reference is not valid for configuration B. Similar part names do not establish interchangeability."),
    ("4. Procedure boundary", "Physical work and restart require authorised site staff and the site's approved procedures. Refer to current site policy for responsibilities. An archived document does not override current safety requirements.")
])
print("Created two simulated source manuals in data/shared_folder/manuals/.")
