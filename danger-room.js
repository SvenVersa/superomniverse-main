// ==================== DANGER ROOM ====================
// Registered Agents & Admins only. Trains a chosen hero against one of
// their *recorded* nemesis_links pairings — only heroes with at least one
// canon nemesis on file show up here at all, per project decision.

const session = requireSession("registered");
const stage = document.getElementById("danger-room-stage");

const STAT_LABELS = {
    intelligence: "INTELLIGENCE",
    strength: "STRENGTH",
    speed: "SPEED",
    durability: "DURABILITY",
    energy_projection: "ENERGY PROJECTION",
    fighting_ability: "FIGHTING ABILITY",
};
const STAT_ORDER = Object.keys(STAT_LABELS);
const MAX_STAT = 7;

let roster = []; // [{hero, nemeses:[{villain, first_encounter_issue}]}]
let pickedHeroId = null;
let pickedVillainId = null;

async function init() {
    if (!session) return;
    document.getElementById("omni-nav-container").innerHTML = renderOmniNav("danger-room");

    try {
        roster = await omniFetch("/game/danger-room-roster");
    } catch (err) {
        stage.innerHTML = `<div class="narrative-box splash-load" style="background:#ffcdd2; color:#b71c1c;">
            💥 COULD NOT LOAD NEMESIS FILES: ${esc(err.message)}
        </div>`;
        return;
    }

    if (roster.length === 0) {
        stage.innerHTML = `<div class="narrative-box splash-load">
            No hero currently has a recorded nemesis with power stats on both sides. An Admin can add a nemesis link
            (and stats for both fighters) from the Admin Console.
        </div>`;
        return;
    }

    pickedHeroId = roster[0].hero.id;
    pickedVillainId = roster[0].nemeses[0].villain.id;
    renderPickerStage();
}

function entryFor(heroId) {
    return roster.find((r) => r.hero.id === heroId);
}

function renderPickerStage() {
    const entry = entryFor(pickedHeroId);

    stage.innerHTML = `
        <div class="comic-entry-banner">
            <div class="entry-sub">★ CHOOSE A HERO WITH A RECORDED NEMESIS ★</div>
            <h2 class="entry-headline">ENTER THE SIMULATION</h2>
        </div>

        <div class="matchup-arena">
            <div class="matchup-side">
                <select id="pick-hero" class="matchup-select">
                    ${roster.map((r) => `<option value="${r.hero.id}" ${r.hero.id === pickedHeroId ? "selected" : ""}>${esc(r.hero.alias)} (${esc(r.hero.publisher)})</option>`).join("")}
                </select>
                <div class="matchup-fighter-card">
                    <div class="matchup-slot-label">HERO</div>
                    <img src="${esc(safeUrl(entry.hero.image))}" alt="${esc(entry.hero.alias)}" data-fb="${esc(entry.hero.alias)}" onerror="omniImgFallback(this)">
                    <div class="matchup-fighter-name">${esc(entry.hero.alias)}</div>
                    <div class="matchup-fighter-sub">${esc(entry.hero.publisher)}</div>
                </div>
            </div>

            <div class="matchup-vs">VS</div>

            <div class="matchup-side">
                <select id="pick-villain" class="matchup-select">
                    ${entry.nemeses.map((n) => `<option value="${n.villain.id}" ${n.villain.id === pickedVillainId ? "selected" : ""}>${esc(n.villain.alias)} (${esc(n.villain.publisher)})</option>`).join("")}
                </select>
                <div class="matchup-fighter-card" id="villain-card"></div>
            </div>
        </div>

        <div id="encounter-note" class="narrative-box" style="max-width:600px; margin: 0 auto 1.5rem auto; text-align:center;"></div>

        <div style="text-align:center; margin-top:1rem;">
            <button id="danger-btn" class="action-btn" style="font-size:1.6rem; padding:0.75rem 2.5rem;">ENTER THE DANGER ROOM</button>
            <div id="danger-error" class="login-error"></div>
        </div>
    `;

    renderVillainCardAndNote(entry);

    document.getElementById("pick-hero").addEventListener("change", (e) => {
        pickedHeroId = Number(e.target.value);
        pickedVillainId = entryFor(pickedHeroId).nemeses[0].villain.id;
        renderPickerStage();
    });
    document.getElementById("pick-villain").addEventListener("change", (e) => {
        pickedVillainId = Number(e.target.value);
        renderVillainCardAndNote(entryFor(pickedHeroId));
    });
    document.getElementById("danger-btn").addEventListener("click", runDangerRoom);
}

function renderVillainCardAndNote(entry) {
    const nemesis = entry.nemeses.find((n) => n.villain.id === pickedVillainId);
    document.getElementById("villain-card").innerHTML = `
        <div class="matchup-slot-label">NEMESIS</div>
        <img src="${esc(safeUrl(nemesis.villain.image))}" alt="${esc(nemesis.villain.alias)}" data-fb="${esc(nemesis.villain.alias)}" onerror="omniImgFallback(this)">
        <div class="matchup-fighter-name">${esc(nemesis.villain.alias)}</div>
        <div class="matchup-fighter-sub">${esc(nemesis.villain.publisher)}</div>
    `;
    const note = document.getElementById("encounter-note");
    note.innerText = nemesis.first_encounter_issue
        ? `FIRST ENCOUNTER ON FILE: ${esc(nemesis.first_encounter_issue)}`
        : "No first-encounter issue on file for this pairing.";
}

async function runDangerRoom() {
    const errorBox = document.getElementById("danger-error");
    errorBox.innerText = "";
    try {
        const result = await omniFetch("/game/danger-room/simulate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ hero_id: pickedHeroId, villain_id: pickedVillainId }),
        });
        renderResult(result);
    } catch (err) {
        errorBox.innerText = err.message;
    }
}

function statBarRow(stat, a, b) {
    const valA = a.stats[stat];
    const valB = b.stats[stat];
    const aWins = valA > valB;
    const bWins = valB > valA;
    return `
        <div class="stat-row">
            <div class="stat-value ${aWins ? "stat-win" : ""}">${valA}</div>
            <div class="stat-track">
                <div class="stat-fill stat-fill-a ${aWins ? "stat-win" : ""}" style="width:${(valA / MAX_STAT) * 50}%"></div>
                <div class="stat-label">${STAT_LABELS[stat]}</div>
                <div class="stat-fill stat-fill-b ${bWins ? "stat-win" : ""}" style="width:${(valB / MAX_STAT) * 50}%"></div>
            </div>
            <div class="stat-value ${bWins ? "stat-win" : ""}">${valB}</div>
        </div>
    `;
}

function renderResult(result) {
    const { fighter_a: hero, fighter_b: villain, winner, first_encounter_issue } = result;
    const heroWins = winner === "a";
    const villainWins = winner === "b";
    const isDraw = winner === null;

    stage.innerHTML = `
        <div class="comic-entry-banner">
            <div class="entry-sub">★ DANGER ROOM SIMULATION LOG ★</div>
            <h2 class="entry-headline">${isDraw ? "STALEMATE!" : heroWins ? "THREAT NEUTRALIZED" : "THE HERO FALLS"}</h2>
        </div>

        ${first_encounter_issue ? `<div class="narrative-box" style="max-width:600px; margin: 0 auto 1.5rem auto; text-align:center;">FIRST ENCOUNTER ON FILE: ${first_encounter_issue}</div>` : ""}

        <div class="matchup-arena matchup-result">
            <div class="matchup-side">
                <div class="matchup-fighter-card ${heroWins ? "winner-glow" : ""}">
                    ${heroWins ? '<div class="winner-stamp">WINNER!</div>' : ""}
                    <img src="${esc(safeUrl(hero.image))}" alt="${esc(hero.alias)}" data-fb="${esc(hero.alias)}" onerror="omniImgFallback(this)">
                    <div class="matchup-fighter-name">${esc(hero.alias)}</div>
                    <div class="matchup-fighter-sub">TOTAL POWER: ${hero.total} / 42</div>
                </div>
            </div>
            <div class="matchup-vs">${isDraw ? "=" : "VS"}</div>
            <div class="matchup-side">
                <div class="matchup-fighter-card ${villainWins ? "winner-glow" : ""}">
                    ${villainWins ? '<div class="winner-stamp">WINNER!</div>' : ""}
                    <img src="${esc(safeUrl(villain.image))}" alt="${esc(villain.alias)}" data-fb="${esc(villain.alias)}" onerror="omniImgFallback(this)">
                    <div class="matchup-fighter-name">${esc(villain.alias)}</div>
                    <div class="matchup-fighter-sub">TOTAL POWER: ${villain.total} / 42</div>
                </div>
            </div>
        </div>

        <div class="stat-compare-box">
            ${STAT_ORDER.map((stat) => statBarRow(stat, hero, villain)).join("")}
        </div>

        <div style="text-align:center; margin-top:2rem;">
            <button id="again-btn" class="action-btn" style="font-size:1.3rem;">RUN ANOTHER SIMULATION</button>
        </div>
    `;

    document.getElementById("again-btn").addEventListener("click", renderPickerStage);
}

init();