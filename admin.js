// ==================== ADMIN CONSOLE ====================
// Admin-only. Full hero CRUD, plus the power_stats and nemesis_links
// management that feeds the Matchup Simulator and Danger Room — if an
// Admin wants a new hero playable in either game, this is where its
// stats and nemesis pairings get entered.

const session = requireSession("admin");
const stage = document.getElementById("admin-stage");

const FIELD_GROUPS = [
    {
        title: "CORE IDENTITY",
        fields: [
            { key: "alias", label: "Alias *", type: "text", required: true },
            { key: "civilian_name", label: "Civilian Name", type: "text" },
            { key: "publisher", label: "Publisher *", type: "select", options: ["Marvel", "DC", "Dynamite", "Image", "Valiant", "Independent"] },
            { key: "alignment", label: "Alignment", type: "text", list: ["Hero", "Villain", "Anti-Hero"] },
            { key: "origin_era", label: "Origin Era", type: "text" },
            { key: "species_or_origin", label: "Species / Origin", type: "text" },
            { key: "image", label: "Image Path", type: "text" },
        ],
    },
    {
        title: "AFFILIATION & THREAT",
        fields: [
            { key: "affiliation", label: "Affiliation / Team", type: "text" },
            { key: "classification", label: "Classification", type: "text" },
            { key: "power_tier", label: "Power Tier", type: "text" },
            { key: "threat_level", label: "Threat Level", type: "text" },
            { key: "base_of_operations", label: "Base of Operations", type: "text" },
            { key: "arch_nemesis", label: "Arch Nemesis (free text)", type: "text" },
            { key: "primary_adversary", label: "Primary Adversary (free text)", type: "text" },
            { key: "signature_gear", label: "Signature Gear", type: "text" },
        ],
    },
    {
        title: "DOSSIER NARRATIVE",
        fields: [
            { key: "primary_powers", label: "Primary Powers", type: "textarea" },
            { key: "tactical_vulnerability", label: "Tactical Vulnerability", type: "textarea" },
            { key: "combat_doctrine", label: "Combat Doctrine", type: "textarea" },
            { key: "psychological_dossier", label: "Psychological Dossier", type: "textarea" },
            { key: "tactical_profile", label: "Tactical Profile", type: "textarea" },
        ],
    },
];

const STAT_FIELDS = ["intelligence", "strength", "speed", "durability", "energy_projection", "fighting_ability"];

let heroes = [];       // full roster, limited columns (id, alias, publisher, affiliation, origin_era, image, civilian_name)
let editingId = null;  // null while creating a brand new dossier

async function init() {
    if (!session) return;
    document.getElementById("omni-nav-container").innerHTML = renderOmniNav("admin");
    await loadHeroes();
    renderTable();
}

async function loadHeroes() {
    heroes = await omniFetch("/heroes");
}

// ==================== ROSTER TABLE ====================
function renderTable() {
    editingId = null;
    stage.innerHTML = `
        <div class="comic-entry-banner">
            <div class="entry-sub">★ OMEGA LEVEL CLEARANCE ★</div>
            <h2 class="entry-headline">DOSSIER MANAGEMENT</h2>
        </div>
        <div style="text-align:right; margin-bottom:1rem;">
            <button id="new-hero-btn" class="action-btn">+ ADD NEW DOSSIER</button>
        </div>
        <div class="admin-table-wrap">
            <table class="admin-table">
                <thead>
                    <tr><th></th><th>ALIAS</th><th>PUBLISHER</th><th>AFFILIATION</th><th>ERA</th><th></th></tr>
                </thead>
                <tbody>
                    ${heroes.map((h) => `
                        <tr>
                            <td><img src="${esc(safeUrl(h.image))}" alt="${esc(h.alias)}" class="admin-row-thumb" data-fb="${esc(h.alias)}" onerror="omniImgFallback(this)"></td>
                            <td>${esc(h.alias)}</td>
                            <td>${esc(h.publisher, "")}</td>
                            <td>${esc(h.affiliation, "")}</td>
                            <td>${esc(h.origin_era, "")}</td>
                            <td class="admin-row-actions">
                                <button class="action-btn admin-edit-btn" data-id="${h.id}">EDIT</button>
                                <button class="action-btn admin-delete-btn" data-id="${h.id}" style="background:var(--ink-black);">DELETE</button>
                            </td>
                        </tr>
                    `).join("")}
                </tbody>
            </table>
        </div>
    `;

    document.getElementById("new-hero-btn").addEventListener("click", () => renderForm(null));
    stage.querySelectorAll(".admin-edit-btn").forEach((btn) => {
        btn.addEventListener("click", () => renderForm(Number(btn.dataset.id)));
    });
    stage.querySelectorAll(".admin-delete-btn").forEach((btn) => {
        btn.addEventListener("click", () => deleteHero(Number(btn.dataset.id)));
    });
}

async function deleteHero(id) {
    const hero = heroes.find((h) => h.id === id);
    if (!confirm(`Permanently purge "${hero ? hero.alias : id}" from the archive?`)) return;
    try {
        await omniFetch(`/heroes/${id}`, { method: "DELETE" });
        await loadHeroes();
        renderTable();
    } catch (err) {
        alert(`Could not delete: ${esc(err.message)}`);
    }
}

// ==================== DOSSIER FORM ====================
async function renderForm(id) {
    editingId = id;
    let hero = {};
    let stats = null;
    let nemeses = [];

    if (id) {
        try {
            [hero, stats, nemeses] = await Promise.all([
                omniFetch(`/heroes/${id}`),
                omniFetch(`/heroes/${id}/stats`),
                omniFetch(`/heroes/${id}/nemeses`),
            ]);
        } catch (err) {
            alert(`Could not load this dossier: ${esc(err.message)}`);
            return;
        }
    }

    stage.innerHTML = `
        <div class="comic-entry-banner">
            <div class="entry-sub">★ ${id ? "EDITING DOSSIER #" + id : "NEW DOSSIER"} ★</div>
            <h2 class="entry-headline">${id ? esc(hero.alias) : "UNTITLED METAHUMAN"}</h2>
        </div>

        <div style="margin-bottom:1rem;">
            <button id="back-btn" class="action-btn" style="background:var(--comic-blue); color:var(--ink-black);">◀ BACK TO ROSTER</button>
        </div>

        <form id="hero-form">
            ${FIELD_GROUPS.map((group) => `
                <fieldset class="admin-fieldset">
                    <legend>${group.title}</legend>
                    <div class="admin-field-grid">
                        ${group.fields.map((f) => fieldHtml(f, hero[f.key])).join("")}
                    </div>
                </fieldset>
            `).join("")}

            <div id="hero-form-error" class="login-error"></div>
            <div style="text-align:center; margin: 1.5rem 0;">
                <button type="submit" class="action-btn" style="font-size:1.3rem;">${id ? "SAVE CHANGES" : "CREATE DOSSIER"}</button>
                ${id ? '<button type="button" id="delete-from-form-btn" class="action-btn" style="background:var(--ink-black); margin-left:1rem;">DELETE DOSSIER</button>' : ""}
            </div>
        </form>

        <fieldset class="admin-fieldset ${id ? "" : "admin-fieldset-disabled"}">
            <legend>POWER STATS (1–7)</legend>
            ${id ? statsFormHtml(stats) : '<p class="admin-disabled-note">Save the dossier first to unlock power stats.</p>'}
        </fieldset>

        <fieldset class="admin-fieldset ${id ? "" : "admin-fieldset-disabled"}">
            <legend>NEMESES</legend>
            ${id ? nemesesFormHtml(id, nemeses) : '<p class="admin-disabled-note">Save the dossier first to unlock nemesis management.</p>'}
        </fieldset>
    `;

    document.getElementById("back-btn").addEventListener("click", renderTable);
    document.getElementById("hero-form").addEventListener("submit", (e) => { e.preventDefault(); saveHero(id); });
    if (id) {
        document.getElementById("delete-from-form-btn").addEventListener("click", () => deleteHero(id));
        document.getElementById("stats-form").addEventListener("submit", (e) => { e.preventDefault(); saveStats(id); });
        wireNemesisControls(id);
    }
}

function fieldHtml(field, value) {
    const val = value == null ? "" : value;
    const id = `field-${field.key}`;
    if (field.type === "select") {
        return `
            <label class="admin-field">
                <span>${field.label}</span>
                <select id="${id}" name="${field.key}">
                    <option value="">-- select --</option>
                    ${field.options.map((o) => `<option value="${esc(o)}" ${val === o ? "selected" : ""}>${esc(o)}</option>`).join("")}
                </select>
            </label>
        `;
    }
    if (field.type === "textarea") {
        return `
            <label class="admin-field admin-field-wide">
                <span>${field.label}</span>
                <textarea id="${id}" name="${field.key}" rows="3">${esc(val)}</textarea>
            </label>
        `;
    }
    const listAttr = field.list ? `list="${id}-list"` : "";
    const datalist = field.list
        ? `<datalist id="${id}-list">${field.list.map((o) => `<option value="${esc(o)}">`).join("")}</datalist>`
        : "";
    return `
        <label class="admin-field">
            <span>${field.label}</span>
            <input type="text" id="${id}" name="${field.key}" value="${esc(val)}" ${listAttr}>
            ${datalist}
        </label>
    `;
}

async function saveHero(id) {
    const form = document.getElementById("hero-form");
    const errorBox = document.getElementById("hero-form-error");
    errorBox.innerText = "";

    const payload = {};
    FIELD_GROUPS.flatMap((g) => g.fields).forEach((f) => {
        const el = form.elements[f.key];
        const value = el.value.trim();
        if (value !== "") payload[f.key] = value;
    });

    if (!payload.alias) {
        errorBox.innerText = "Alias is required.";
        return;
    }

    try {
        if (id) {
            await omniFetch(`/heroes/${id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            await loadHeroes();
            renderForm(id);
        } else {
            const created = await omniFetch("/heroes", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            await loadHeroes();
            renderForm(created.id);
        }
    } catch (err) {
        errorBox.innerText = err.message;
    }
}

// ==================== POWER STATS SUBFORM ====================
function statsFormHtml(stats) {
    const s = stats || {};
    return `
        <form id="stats-form">
            <div class="admin-field-grid">
                ${STAT_FIELDS.map((key) => `
                    <label class="admin-field">
                        <span>${key.replace(/_/g, " ").toUpperCase()}</span>
                        <input type="number" min="1" max="7" name="${key}" value="${s[key] != null ? s[key] : ""}" required>
                    </label>
                `).join("")}
            </div>
            <div id="stats-form-error" class="login-error"></div>
            <div style="text-align:center; margin-top:1rem;">
                <button type="submit" class="action-btn">SAVE STATS</button>
            </div>
        </form>
    `;
}

async function saveStats(heroId) {
    const form = document.getElementById("stats-form");
    const errorBox = document.getElementById("stats-form-error");
    errorBox.innerText = "";

    const payload = {};
    STAT_FIELDS.forEach((key) => { payload[key] = Number(form.elements[key].value); });

    try {
        await omniFetch(`/heroes/${heroId}/stats`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        errorBox.style.color = "green";
        errorBox.innerText = "Stats saved.";
    } catch (err) {
        errorBox.style.color = "";
        errorBox.innerText = err.message;
    }
}

// ==================== NEMESES SUBFORM ====================
function nemesesFormHtml(heroId, nemeses) {
    const otherHeroes = heroes.filter((h) => h.id !== heroId);
    return `
        <div class="admin-nemesis-list">
            ${nemeses.length === 0 ? '<p class="admin-disabled-note">No nemeses recorded yet.</p>' : nemeses.map((n) => `
                <div class="admin-nemesis-row">
                    <span><strong>${esc(n.villain_alias)}</strong>${n.first_encounter_issue ? ` &mdash; ${esc(n.first_encounter_issue)}` : ""}</span>
                    <button type="button" class="action-btn admin-remove-nemesis-btn" data-villain-id="${n.villain_id}" style="background:var(--ink-black);">REMOVE</button>
                </div>
            `).join("")}
        </div>
        <form id="add-nemesis-form" class="admin-field-grid" style="margin-top:1rem;">
            <label class="admin-field">
                <span>ADD NEMESIS</span>
                <select name="villain_id" required>
                    <option value="">-- select a dossier --</option>
                    ${otherHeroes.map((h) => `<option value="${h.id}">${esc(h.alias)} (${esc(h.publisher, "?")})</option>`).join("")}
                </select>
            </label>
            <label class="admin-field">
                <span>FIRST ENCOUNTER ISSUE (optional)</span>
                <input type="text" name="first_encounter_issue" placeholder="e.g. Amazing Fantasy #15">
            </label>
            <div style="align-self:flex-end;">
                <button type="submit" class="action-btn">ADD</button>
            </div>
        </form>
        <div id="nemesis-form-error" class="login-error"></div>
    `;
}

function wireNemesisControls(heroId) {
    stage.querySelectorAll(".admin-remove-nemesis-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
            try {
                await omniFetch(`/heroes/${heroId}/nemeses/${btn.dataset.villainId}`, { method: "DELETE" });
                renderForm(heroId);
            } catch (err) {
                alert(`Could not remove nemesis: ${esc(err.message)}`);
            }
        });
    });

    const addForm = document.getElementById("add-nemesis-form");
    addForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const errorBox = document.getElementById("nemesis-form-error");
        errorBox.innerText = "";
        const villainId = Number(addForm.elements["villain_id"].value);
        const issue = addForm.elements["first_encounter_issue"].value.trim();
        if (!villainId) {
            errorBox.innerText = "Pick a dossier to link as a nemesis.";
            return;
        }
        try {
            await omniFetch(`/heroes/${heroId}/nemeses`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ villain_id: villainId, first_encounter_issue: issue || null }),
            });
            renderForm(heroId);
        } catch (err) {
            errorBox.innerText = err.message;
        }
    });
}

init();     