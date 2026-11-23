// ==================== INTEL LOG ====================
// Registered Agents & Admins only. This is the registered tier's own
// perk beyond just viewing full dossiers: agents file and read field
// reports on heroes (intel_reports), each tied to the agent who filed it.

const session = requireSession("registered");
const stage = document.getElementById("intel-stage");

const THREAT_OPTIONS = ["Monitoring", "Elevated", "Critical", "Neutralized"];

let heroes = [];
let reports = [];

function fallbackImg(alias) {
    return `https://via.placeholder.com/60x60/101012/fff?text=${encodeURIComponent((alias || "?")[0])}`;
}

function formatDate(iso) {
    try {
        return new Date(iso).toLocaleString();
    } catch (e) {
        return iso;
    }
}

function threatClass(status) {
    const key = (status || "").toLowerCase();
    if (key === "critical") return "threat-critical";
    if (key === "elevated") return "threat-elevated";
    if (key === "neutralized") return "threat-neutralized";
    return "threat-monitoring";
}

async function init() {
    if (!session) return;
    document.getElementById("omni-nav-container").innerHTML = renderOmniNav("intel");

    try {
        [heroes, reports] = await Promise.all([
            omniFetch("/heroes"),
            omniFetch("/intel-reports"),
        ]);
    } catch (err) {
        stage.innerHTML = `<div class="narrative-box splash-load" style="background:#ffcdd2; color:#b71c1c;">
            💥 COULD NOT LOAD THE INTEL LOG: ${err.message}
        </div>`;
        return;
    }

    render();
}

function render() {
    stage.innerHTML = `
        <div class="comic-entry-banner">
            <div class="entry-sub">★ CLASSIFIED — AGENT EYES ONLY ★</div>
            <h2 class="entry-headline">FIELD INTEL</h2>
        </div>

        <fieldset class="admin-fieldset">
            <legend>FILE NEW REPORT</legend>
            <form id="new-report-form">
                <div class="admin-field-grid">
                    <label class="admin-field">
                        <span>HERO</span>
                        <select name="hero_id" required>
                            <option value="">-- select a dossier --</option>
                            ${heroes.map((h) => `<option value="${h.id}">${h.alias}</option>`).join("")}
                        </select>
                    </label>
                    <label class="admin-field">
                        <span>THREAT STATUS</span>
                        <input type="text" name="threat_status" list="threat-options" value="Monitoring">
                        <datalist id="threat-options">${THREAT_OPTIONS.map((o) => `<option value="${o}">`).join("")}</datalist>
                    </label>
                    <label class="admin-field admin-field-wide">
                        <span>REPORT</span>
                        <textarea name="report_text" rows="3" required placeholder="Field observations, tactical notes, sightings..."></textarea>
                    </label>
                </div>
                <div id="new-report-error" class="login-error"></div>
                <div style="text-align:center; margin-top:1rem;">
                    <button type="submit" class="action-btn">FILE REPORT</button>
                </div>
            </form>
        </fieldset>

        <div id="report-feed" class="intel-feed"></div>
    `;

    document.getElementById("new-report-form").addEventListener("submit", submitNewReport);
    renderFeed();
}

function renderFeed() {
    const feed = document.getElementById("report-feed");
    if (reports.length === 0) {
        feed.innerHTML = '<p class="admin-disabled-note" style="text-align:center;">No intel filed yet. Be the first agent on record.</p>';
        return;
    }

    feed.innerHTML = reports.map((r) => {
        const canManage = session.role === "admin" || r.agent_username === session.username;
        return `
            <article class="intel-card" id="intel-card-${r.report_id}">
                <img src="${r.hero_image}" alt="${r.hero_alias}" onerror="this.onerror=null; this.src='${fallbackImg(r.hero_alias)}';">
                <div class="intel-card-body">
                    <div class="intel-card-head">
                        <span class="intel-hero-name">${r.hero_alias}</span>
                        <span class="threat-badge ${threatClass(r.threat_status)}">${r.threat_status}</span>
                    </div>
                    <p class="intel-report-text">${r.report_text}</p>
                    <div class="intel-card-meta">
                        FILED BY <strong>${r.agent_username}</strong> &bull; ${formatDate(r.filed_at)}
                    </div>
                    ${canManage ? `
                        <div class="intel-card-actions">
                            <button class="action-btn intel-edit-btn" data-id="${r.report_id}" style="background:var(--comic-blue); color:var(--ink-black);">EDIT</button>
                            <button class="action-btn intel-delete-btn" data-id="${r.report_id}" style="background:var(--ink-black);">DELETE</button>
                        </div>
                    ` : ""}
                </div>
            </article>
        `;
    }).join("");

    feed.querySelectorAll(".intel-edit-btn").forEach((btn) => {
        btn.addEventListener("click", () => startEdit(Number(btn.dataset.id)));
    });
    feed.querySelectorAll(".intel-delete-btn").forEach((btn) => {
        btn.addEventListener("click", () => deleteReport(Number(btn.dataset.id)));
    });
}

async function submitNewReport(e) {
    e.preventDefault();
    const form = e.target;
    const errorBox = document.getElementById("new-report-error");
    errorBox.innerText = "";

    const heroId = Number(form.elements["hero_id"].value);
    const threatStatus = form.elements["threat_status"].value.trim() || "Monitoring";
    const reportText = form.elements["report_text"].value.trim();

    if (!heroId || !reportText) {
        errorBox.innerText = "Pick a hero and write a report before filing.";
        return;
    }

    try {
        await omniFetch("/intel-reports", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ hero_id: heroId, threat_status: threatStatus, report_text: reportText }),
        });
        reports = await omniFetch("/intel-reports");
        render();
    } catch (err) {
        errorBox.innerText = err.message;
    }
}

function startEdit(reportId) {
    const report = reports.find((r) => r.report_id === reportId);
    const card = document.getElementById(`intel-card-${reportId}`);
    card.querySelector(".intel-card-body").innerHTML = `
        <form class="intel-edit-form">
            <label class="admin-field">
                <span>THREAT STATUS</span>
                <input type="text" name="threat_status" list="threat-options" value="${report.threat_status}">
            </label>
            <label class="admin-field admin-field-wide">
                <span>REPORT</span>
                <textarea name="report_text" rows="3">${report.report_text}</textarea>
            </label>
            <div id="edit-error-${reportId}" class="login-error"></div>
            <div class="intel-card-actions">
                <button type="submit" class="action-btn">SAVE</button>
                <button type="button" class="action-btn cancel-edit-btn" style="background:var(--comic-blue); color:var(--ink-black);">CANCEL</button>
            </div>
        </form>
    `;
    card.querySelector(".intel-edit-form").addEventListener("submit", (e) => saveEdit(e, reportId));
    card.querySelector(".cancel-edit-btn").addEventListener("click", renderFeed);
}

async function saveEdit(e, reportId) {
    e.preventDefault();
    const form = e.target;
    const errorBox = document.getElementById(`edit-error-${reportId}`);
    const threatStatus = form.elements["threat_status"].value.trim();
    const reportText = form.elements["report_text"].value.trim();

    if (!reportText) {
        errorBox.innerText = "Report text cannot be empty.";
        return;
    }

    try {
        await omniFetch(`/intel-reports/${reportId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ threat_status: threatStatus, report_text: reportText }),
        });
        reports = await omniFetch("/intel-reports");
        renderFeed();
    } catch (err) {
        errorBox.innerText = err.message;
    }
}

async function deleteReport(reportId) {
    if (!confirm("Delete this intel report? This cannot be undone.")) return;
    try {
        await omniFetch(`/intel-reports/${reportId}`, { method: "DELETE" });
        reports = reports.filter((r) => r.report_id !== reportId);
        renderFeed();
    } catch (err) {
        alert(`Could not delete: ${err.message}`);
    }
}

init();