// ====================================================================
// SHARED ACROSS EVERY OMNIVERSE PAGE
// Session lives in sessionStorage (cleared when the tab closes) so moving
// between the Archive, Matchup Simulator, Danger Room, Intel Log, and
// Admin Console doesn't force a fresh login on every page. Guests are
// never written here any differently than agents — it's the same
// "public_guest_clearance" key the API already treats as a non-account.
// ====================================================================

const API_URL = (location.hostname === "127.0.0.1" || location.hostname === "localhost")
    ? "http://127.0.0.1:8000/api"
    : "/api";

const Session = {
    KEY: "omniverse_session",

    save(apiKey, role, username) {
        try {
            sessionStorage.setItem(this.KEY, JSON.stringify({ apiKey, role, username }));
        } catch (e) { /* storage unavailable — session just won't carry over */ }
    },

    load() {
        try {
            const raw = sessionStorage.getItem(this.KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (e) {
            return null;
        }
    },

    clear() {
        try { sessionStorage.removeItem(this.KEY); } catch (e) {}
    },

    logout() {
        this.clear();
        window.location.href = "index.html";
    },
};

// Call at the top of any page besides index.html. Redirects to the login
// screen if there's no session, or if the session's role doesn't meet
// `minRole` ("registered" = user or admin, "admin" = admin only).
// Returns the session object ({apiKey, role}) so the caller can use it.
function requireSession(minRole) {
    const session = Session.load();
    if (!session) {
        window.location.href = "index.html";
        return null;
    }
    if (minRole === "registered" && session.role === "guest") {
        window.location.href = "index.html";
        return null;
    }
    if (minRole === "admin" && session.role !== "admin") {
        window.location.href = "index.html";
        return null;
    }
    return session;
}

// Shared fetch helper: attaches the clearance header and gives back a
// parsed error message on failure instead of throwing a raw Response.
async function omniFetch(path, options = {}) {
    const session = Session.load();
    const headers = Object.assign(
        { "x-api-key": session ? session.apiKey : "" },
        options.headers || {}
    );
    const response = await fetch(`${API_URL}${path}`, { ...options, headers });
    let body = null;
    try { body = await response.json(); } catch (e) { /* no JSON body */ }
    if (!response.ok) {
        let detail = `Request failed (${response.status})`;
        if (body && typeof body.detail === "string") {
            detail = body.detail;
        } else if (body && Array.isArray(body.detail)) {
            // FastAPI/Pydantic validation errors come back as a list of
            // {loc, msg, type} objects rather than a plain string.
            detail = body.detail.map((d) => {
                const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : "";
                return field ? `${field}: ${d.msg}` : d.msg;
            }).join("; ");
        }
        const err = new Error(detail);
        err.status = response.status;
        throw err;
    }
    return body;
}

// Renders the shared nav strip used on every page once logged in.
// `active` names the current page so its button highlights.
function renderOmniNav(active) {
    const session = Session.load();
    if (!session) return "";
    const isRegistered = session.role !== "guest";
    const isAdmin = session.role === "admin";

    const link = (href, label, key) =>
        `<a href="${href}" class="omni-nav-btn${active === key ? " active" : ""}">${label}</a>`;

    return `
        <nav class="omni-nav-bar">
            <div class="omni-nav-links">
                ${link("index.html", "📖 ARCHIVE", "archive")}
                ${isRegistered ? link("matchup.html", "⚔️ MATCHUP SIM", "matchup") : ""}
                ${isRegistered ? link("danger-room.html", "☠️ DANGER ROOM", "danger-room") : ""}
                ${isRegistered ? link("intel.html", "📋 INTEL LOG", "intel") : ""}
                ${isAdmin ? link("admin.html", "🛡️ ADMIN CONSOLE", "admin") : ""}
            </div>
            <div class="omni-nav-session">
                <span class="omni-nav-role omni-role-${session.role}">${session.username ? session.username.toUpperCase() + " • " : ""}${session.role.toUpperCase()}</span>
                <button type="button" class="omni-nav-btn omni-nav-logout" onclick="Session.logout()">END SESSION</button>
            </div>
        </nav>
    `;
}

// Standard "you don't have clearance for this" splash used by any page a
// guest lands on directly (e.g. by typing the URL).
function renderClearanceDenied(message) {
    return `
        <div class="comic-page-sheet" style="text-align:center; padding: 4rem 2rem;">
            <h2 style="font-family:'Bangers', cursive; font-size:4rem; color:var(--comic-red);">ACCESS DENIED</h2>
            <p style="font-family:'Comic Neue', cursive; font-size:1.3rem; font-weight:bold; max-width: 520px; margin: 1rem auto;">
                ${message}
            </p>
            <a href="index.html" class="action-btn" style="display:inline-block; margin-top:1.5rem; text-decoration:none;">RETURN TO ARCHIVE</a>
        </div>
    `;
}