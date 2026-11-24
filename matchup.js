const session = requireSession("registered");
const stage = document.getElementById("matchup-stage");

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

let roster = [];
let pickA = null;
let pickB = null;

async function init() {
    if (!session) return;
    document.getElementById("omni-nav-container").innerHTML = renderOmniNav("matchup");

    try {
        roster = await omniFetch("/game/matchup-roster");
    } catch (err) {
        stage.innerHTML = `<div class="narrative-box splash-load" style="background:#ffcdd2; color:#b71c1c;">
            💥 COULD NOT LOAD THE ROSTER: ${esc(err.message)}
        </div>`;
        return;
    }

    if (roster.length < 2) {
        stage.innerHTML = `<div class="narrative-box splash-load">
            Only ${roster.length} combatant(s) have power stats on file. An Admin needs to add stats to at least two
            dossiers in the Admin Console before a matchup can run.
        </div>`;
        return;
    }

    renderPickerStage();
}

function optionsHtml(excludeId) {
    return roster
        .filter((f) => f.id !== excludeId)
        .map((f) => `<option value="${f.id}">${esc(f.alias)} (${esc(f.publisher)})</option>`)
        .join("");
}

function fighterById(id) {
    return roster.find((f) => f.id === Number(id));
}

function fighterCardHtml(fighter, slotLabel) {
    if (!fighter) {
        return `
            <div class="matchup-slot-empty">
                <div class="matchup-slot-label">${slotLabel}</div>
                <div class="matchup-slot-placeholder">SELECT A COMBATANT ▼</div>
            </div>
        `;
    }
    return `
        <div class="matchup-fighter-card">
            <div class="matchup-slot-label">${slotLabel}</div>
            <img src="${esc(safeUrl(fighter.image))}" alt="${esc(fighter.alias)}" data-fb="${esc(fighter.alias)}" onerror="omniImgFallback(this)">
            <div class="matchup-fighter-name">${esc(fighter.alias)}</div>
            <div class="matchup-fighter-sub">${esc(fighter.publisher)} &bull; ${esc(fighter.alignment, "Unaligned")}</div>
        </div>
    `;
}

function renderPickerStage() {
    stage.innerHTML = `
        <div class="comic-entry-banner">
            <div class="entry-sub">★ PICK ANY TWO DOSSIERS ★</div>
            <h2 class="entry-headline">INITIATE A SIMULATION</h2>
        </div>

        <div class="matchup-arena">
            <div class="matchup-side">
                <select id="pick-a" class="matchup-select">
                    <option value="">-- CHOOSE FIGHTER A --</option>
                    ${optionsHtml(pickB)}
                </select>
                <div id="card-a">${fighterCardHtml(null, "FIGHTER A")}</div>
            </div>

            <div class="matchup-vs">VS</div>

            <div class="matchup-side">
                <select id="pick-b" class="matchup-select">
                    <option value="">-- CHOOSE FIGHTER B --</option>
                    ${optionsHtml(pickA)}
                </select>
                <div id="card-b">${fighterCardHtml(null, "FIGHTER B")}</div>
            </div>
        </div>

        <div style="text-align:center; margin-top:2rem;">
            <button id="fight-btn" class="action-btn" style="font-size:1.6rem; padding:0.75rem 2.5rem;" disabled>FIGHT!</button>
            <div id="matchup-error" class="login-error"></div>
        </div>
    `;

    const selectA = document.getElementById("pick-a");
    const selectB = document.getElementById("pick-b");
    const fightBtn = document.getElementById("fight-btn");

    if (pickA) selectA.value = pickA;
    if (pickB) selectB.value = pickB;
    document.getElementById("card-a").innerHTML = fighterCardHtml(fighterById(pickA), "FIGHTER A");
    document.getElementById("card-b").innerHTML = fighterCardHtml(fighterById(pickB), "FIGHTER B");
    fightBtn.disabled = !(pickA && pickB);

    selectA.addEventListener("change", (e) => {
        pickA = e.target.value ? Number(e.target.value) : null;
        renderPickerStage();
    });
    selectB.addEventListener("change", (e) => {
        pickB = e.target.value ? Number(e.target.value) : null;
        renderPickerStage();
    });
    fightBtn.addEventListener("click", runMatchup);
}

async function runMatchup() {
    const errorBox = document.getElementById("matchup-error");
    errorBox.innerText = "";
    try {
        const result = await omniFetch("/game/matchup", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ hero_a_id: pickA, hero_b_id: pickB }),
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
    const { fighter_a: a, fighter_b: b, winner } = result;
    const aIsWinner = winner === "a";
    const bIsWinner = winner === "b";
    const isDraw = winner === null;

    stage.innerHTML = `
        <div class="comic-entry-banner">
            <div class="entry-sub">★ SIMULATION COMPLETE ★</div>
            <h2 class="entry-headline">${isDraw ? "IT'S A DRAW!" : "WE HAVE A WINNER"}</h2>
        </div>

        <div class="matchup-arena matchup-result">
            <div class="matchup-side">
                <div class="matchup-fighter-card ${aIsWinner ? "winner-glow" : ""}">
                    ${aIsWinner ? '<div class="winner-stamp">WINNER!</div>' : ""}
                    <img src="${esc(safeUrl(a.image))}" alt="${esc(a.alias)}" data-fb="${esc(a.alias)}" onerror="omniImgFallback(this)">
                    <div class="matchup-fighter-name">${esc(a.alias)}</div>
                    <div class="matchup-fighter-sub">TOTAL POWER: ${a.total} / 42</div>
                </div>
            </div>
            <div class="matchup-vs">${isDraw ? "=" : "VS"}</div>
            <div class="matchup-side">
                <div class="matchup-fighter-card ${bIsWinner ? "winner-glow" : ""}">
                    ${bIsWinner ? '<div class="winner-stamp">WINNER!</div>' : ""}
                    <img src="${esc(safeUrl(b.image))}" alt="${esc(b.alias)}" data-fb="${esc(b.alias)}" onerror="omniImgFallback(this)">
                    <div class="matchup-fighter-name">${esc(b.alias)}</div>
                    <div class="matchup-fighter-sub">TOTAL POWER: ${b.total} / 42</div>
                </div>
            </div>
        </div>

        <div class="stat-compare-box">
            ${STAT_ORDER.map((stat) => statBarRow(stat, a, b)).join("")}
        </div>

        <div style="text-align:center; margin-top:2rem;">
            <button id="rematch-btn" class="action-btn" style="font-size:1.3rem;">RUN ANOTHER MATCHUP</button>
        </div>
    `;

    document.getElementById("rematch-btn").addEventListener("click", () => {
        renderPickerStage();
    });
}

init();