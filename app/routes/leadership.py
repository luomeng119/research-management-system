from flask import Blueprint, render_template


bp = Blueprint("leadership", __name__)


@bp.get("/system-intro")
def system_intro():
    return render_template("leadership/system_intro.html")
