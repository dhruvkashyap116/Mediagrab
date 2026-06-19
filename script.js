/**
 * MediaGrab — Zero-friction frontend
 * Auto-fetch on paste, smart defaults, minimal clicks
 */

const API = '';
const POLL_INTERVAL = 800;
const DEBOUNCE_MS = 600;

// ─── DOM ───
const urlInput = document.getElementById('urlInput');
const btnClear = document.getElementById('btnClear');
const btnDownload = document.getElementById('btnDownload');
const inputLoader = document.getElementById('inputLoader');
const preview = document.getElementById('preview');
const options = document.getElementById('options');
const qualitySelect = document.getElementById('qualitySelect');
const ratioSelect = document.getElementById('ratioSelect');
const audioFormatSelect = document.getElementById('audioFormatSelect');
const qualityGroup = document.getElementById('qualityGroup');
const ratioGroup = document.getElementById('ratioGroup');
const audioGroup = document.getElementById('audioGroup');
const dlList = document.getElementById('dlList');
const emptyState = document.getElementById('emptyState');
// ─── Setup Lenis Smooth Scroll ───
try {
    if (typeof Lenis !== 'undefined') {
        const lenis = new Lenis({
            lerp: 0.08, // Adjust inertia (lower is smoother)
            wheelMultiplier: 1.1, // Slight scroll speed bump
            smoothWheel: true,
            touchMultiplier: 1.5,
        });
        function raf(time) {
            lenis.raf(time);
            requestAnimationFrame(raf);
        }
        requestAnimationFrame(raf);
    }
} catch (e) {
    console.warn("Lenis smooth scroll failed to load.");
}
const statusBar = document.getElementById('statusBar');
const cookieFileInput = document.getElementById('cookieFileInput');
const toastContainer = document.getElementById('toastContainer');

let currentFormat = 'video';
let activeTasks = {};
let fetchTimer = null;
let lastFetchedUrl = '';

// ─── Format Toggle ───
document.querySelectorAll('.fmt-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.fmt-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentFormat = btn.dataset.format;

        qualityGroup.classList.toggle('hidden', currentFormat === 'audio');
        ratioGroup.classList.toggle('hidden', currentFormat === 'audio');
        audioGroup.classList.toggle('hidden', currentFormat === 'video');
    });
});

// ─── Clear ───
btnClear.addEventListener('click', () => {
    urlInput.value = '';
    urlInput.focus();
    hidePreview();
    lastFetchedUrl = '';
});

// ─── Auto-fetch on paste / input ───
urlInput.addEventListener('input', () => {
    const url = urlInput.value.trim();
    if (!url || !isValidUrl(url)) {
        preview.classList.remove('visible');
        return;
    }
    clearTimeout(fetchTimer);
    fetchTimer = setTimeout(() => autoFetch(url), DEBOUNCE_MS);
});

// Also detect paste event for instant response
urlInput.addEventListener('paste', (e) => {
    setTimeout(() => {
        const url = urlInput.value.trim();
        if (url && isValidUrl(url)) {
            clearTimeout(fetchTimer);
            autoFetch(url);
        }
    }, 50);
});

// Enter key → download
urlInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        e.preventDefault();
        if (preview.classList.contains('visible')) {
            startDownload();
        }
    }
});

async function autoFetch(url) {
    if (url === lastFetchedUrl) return;
    lastFetchedUrl = url;

    // Show loading indicator
    inputLoader.className = 'input-loader loading';

    try {
        const res = await fetch(`${API}/api/info`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
        const data = await res.json();

        if (data.error) {
            inputLoader.className = 'input-loader';
            return;
        }

        inputLoader.className = 'input-loader done';
        setTimeout(() => { inputLoader.className = 'input-loader'; }, 500);

        showPreview(data);
    } catch {
        inputLoader.className = 'input-loader';
    }
}

function showPreview(data) {
    document.getElementById('previewThumb').src = data.thumbnail || '';
    document.getElementById('previewTitle').textContent = data.title || 'Unknown';
    document.getElementById('previewUploader').textContent = data.uploader || '';
    document.getElementById('previewDuration').textContent = formatDuration(data.duration);
    document.getElementById('previewViews').textContent = data.view_count ? formatViews(data.view_count) + ' views' : '';

    const badges = document.getElementById('previewBadges');
    badges.innerHTML = (data.resolutions || []).slice(0, 6).map(r =>
        `<span class="badge">${r.label}</span>`
    ).join('');

    preview.classList.add('visible');
}

// ─── Download ───
btnDownload.addEventListener('click', startDownload);

async function startDownload() {
    const url = urlInput.value.trim();
    if (!url) return toast('Please enter a URL', 'error');
    if (!isValidUrl(url)) return toast('Enter a valid URL', 'error');

    btnDownload.disabled = true;
    const orig = btnDownload.innerHTML;
    btnDownload.innerHTML = '<div class="spinner"></div> Starting…';

    try {
        const body = {
            url,
            type: currentFormat,
            quality: qualitySelect.value,
            audioFormat: audioFormatSelect.value,
            ratio: ratioSelect.value,
        };

        const res = await fetch(`${API}/api/download`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        toast(data.cached ? '⚡ Ready instantly (cached)' : 'Download started', 'success');
        activeTasks[data.task_id] = currentFormat;
        addDlItem(data.task_id, url, currentFormat);
        pollStatus(data.task_id);
        emptyState.style.display = 'none';

    } catch (err) {
        toast(humanError(err.message), 'error');
    } finally {
        btnDownload.disabled = false;
        btnDownload.innerHTML = orig;
    }
}

function addDlItem(taskId, url, type) {
    const icons = {
        video: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>',
        audio: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
        image: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>',
    };
    const hostname = (() => { try { return new URL(url).hostname; } catch { return url.slice(0, 40); } })();

    const el = document.createElement('div');
    el.className = 'dl-item';
    el.id = `dl-${taskId}`;
    el.innerHTML = `
        <div class="dl-type">${icons[type] || icons.video}</div>
        <div class="dl-body">
            <div class="dl-name" id="dn-${taskId}">${hostname}</div>
            <div class="dl-info">
                <span class="dl-tag queued" id="dt-${taskId}">Queued</span>
                <span class="dl-detail" id="dd-${taskId}">Waiting…</span>
            </div>
            <div class="dl-bar"><div class="dl-fill" id="dp-${taskId}" style="width:0%"></div></div>
        </div>
        <div class="dl-actions" id="da-${taskId}"></div>
    `;
    dlList.prepend(el);
}

// ─── History Management ───
const HISTORY_KEY = 'mediagrab_history';
function loadHistory() {
    const history = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
    const now = Date.now();
    const validHistory = history.filter(h => now - h.timestamp < 30 * 60 * 1000);

    if (validHistory.length !== history.length) {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(validHistory));
    }

    if (validHistory.length > 0) {
        const emptyState = document.getElementById('emptyState');
        if (emptyState) emptyState.remove();
        validHistory.forEach(h => {
            if (!document.getElementById(`dl-${h.taskId}`)) {
                const temp = document.createElement('div');
                temp.innerHTML = h.html;
                dlList.appendChild(temp.firstElementChild);
            }
        });
    }
}

function saveToHistory(taskId, html) {
    const history = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
    const existingIndex = history.findIndex(h => h.taskId === taskId);
    if (existingIndex >= 0) {
        history[existingIndex].html = html;
    } else {
        history.unshift({ taskId, html, timestamp: Date.now() });
    }
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
}

document.getElementById('btnClearHistory')?.addEventListener('click', () => {
    localStorage.removeItem(HISTORY_KEY);
    document.getElementById('dlList').innerHTML = `<div class="empty" id="emptyState"><p>No downloads yet</p><span>Paste a URL above to get started</span></div>`;
    toast('History cleared', 'success');
});

const PHASES = {
    queued: 'Queued', starting: 'Starting…', fetching: 'Fetching…',
    downloading: 'Downloading', processing: 'Processing…',
    complete: 'Complete', error: 'Failed', expired: 'Expired',
};

async function pollStatus(taskId) {
    try {
        const res = await fetch(`${API}/api/status/${taskId}`);
        const data = await res.json();

        const nameEl = document.getElementById(`dn-${taskId}`);
        const tagEl = document.getElementById(`dt-${taskId}`);
        const detailEl = document.getElementById(`dd-${taskId}`);
        const barEl = document.getElementById(`dp-${taskId}`);
        const actEl = document.getElementById(`da-${taskId}`);
        if (!tagEl) return;

        if (data.title) nameEl.textContent = data.title;

        const phase = PHASES[data.phase] || PHASES[data.status] || data.status;
        tagEl.textContent = phase;
        tagEl.className = `dl-tag ${data.status}`;
        barEl.style.width = data.progress + '%';

        if (data.status === 'downloading') {
            const parts = [data.progress + '%'];
            if (data.speed) parts.push(formatBytes(data.speed) + '/s');
            if (data.eta) parts.push(formatETA(data.eta));
            detailEl.textContent = parts.join(' · ');
        }

        if (data.status === 'complete') {
            const parts = [formatBytes(data.filesize)];
            if (data.resolution && data.resolution !== '0x0') parts.push(data.resolution);
            if (data.fps) parts.push(data.fps + 'fps');
            if (data.expires_in > 0) parts.push('expires ' + formatETA(data.expires_in));
            detailEl.textContent = parts.join(' · ');

            const link = `${API}/api/download/${taskId}/file`;
            actEl.innerHTML = `
                <button class="dl-btn" title="Copy link" onclick="copyLink('${link}')">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                </button>
                <a href="${link}" class="dl-btn" title="Save file">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                </a>
            `;
            delete activeTasks[taskId];
            const el = document.getElementById(`dl-${taskId}`);
            if (el) saveToHistory(taskId, el.outerHTML);
            toast(`"${data.title}" ready!`, 'success');
            return;
        }

        if (data.status === 'error') {
            detailEl.textContent = humanError(data.error);
            delete activeTasks[taskId];
            const el = document.getElementById(`dl-${taskId}`);
            if (el) saveToHistory(taskId, el.outerHTML);
            toast(humanError(data.error), 'error');
            return;
        }

        if (data.status === 'expired') {
            detailEl.textContent = 'File cleaned up';
            actEl.innerHTML = '';
            return;
        }

        setTimeout(() => pollStatus(taskId), POLL_INTERVAL);
    } catch {
        setTimeout(() => pollStatus(taskId), POLL_INTERVAL * 2);
    }
}

// ─── Helpers ───
function isValidUrl(s) {
    try {
        const u = new URL(s);
        if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
        // Require more than just a bare domain (e.g. youtu.be needs /videoId)
        if (u.pathname.length <= 1 && !u.search) return false;
        return true;
    }
    catch { return false; }
}

function copyLink(url) {
    navigator.clipboard.writeText(window.location.origin + url)
        .then(() => toast('Link copied!'))
        .catch(() => toast('Could not copy', 'error'));
}

function humanError(msg) {
    if (!msg) return 'Something went wrong';
    if (msg.includes('Sign in') || msg.includes('bot'))
        return 'This video requires authentication. Upload a cookies.txt file.';
    if (msg.includes('Incomplete') || msg.includes('no video') || msg.includes('Unsupported URL'))
        return 'Paste a full video URL (e.g. youtu.be/dQw4w9WgXcQ)';
    if (msg.includes('too large') || msg.includes('Max'))
        return msg;
    if (msg.includes('private') || msg.includes('not available') || msg.includes('unavailable'))
        return 'This video is private or unavailable';
    if (msg.includes('Rate limit'))
        return 'Too many requests — please wait a moment';
    if (msg.includes('timed out'))
        return 'Download timed out — try a lower quality';
    if (msg.length > 120)
        return msg.slice(0, 120) + '…';
    return msg;
}

function formatBytes(b) {
    if (!b) return '0 B';
    const s = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(b) / Math.log(1024));
    return (b / Math.pow(1024, i)).toFixed(1) + ' ' + s[i];
}

function formatDuration(sec) {
    if (!sec) return '';
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

function formatETA(sec) {
    if (!sec) return '';
    return sec < 60 ? `${Math.round(sec)}s` : `${Math.round(sec / 60)}m`;
}

function formatViews(n) {
    if (!n) return '0';
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toString();
}

function toast(msg, type = 'info') {
    const t = document.createElement('div');
    t.className = `toast ${type}`;
    t.textContent = msg;
    toastContainer.appendChild(t);
    setTimeout(() => { t.style.animation = 'toastOut 0.25s ease forwards'; setTimeout(() => t.remove(), 250); }, 3500);
}

// ─── System Status ───
async function checkStatus() {
    try {
        const res = await fetch(`${API}/api/system-status`);
        const data = await res.json();
        if (!data.cookies_file) {
            statusBar.className = 'status-bar warn';
            statusBar.innerHTML = `
                ⚠️ <span>No cookies — YouTube may block downloads.</span>
                <button class="btn-cookie" id="btnCookie">Upload cookies.txt</button>
                <button class="btn-cookie btn-cookie-help" id="btnCookieHelp">How to get cookies?</button>
            `;
            document.getElementById('btnCookie').addEventListener('click', () => cookieFileInput.click());
            document.getElementById('btnCookieHelp').addEventListener('click', showCookieGuide);
        } else if (data.ffmpeg && data.cookies_file) {
            statusBar.className = 'status-bar ok';
            statusBar.textContent = '✓ Ready — all systems go';
            setTimeout(() => { statusBar.innerHTML = ''; statusBar.className = 'status-bar'; }, 4000);
        }
    } catch { }
}

function showCookieGuide() {
    const overlay = document.getElementById('modalOverlay');
    // Create cookie guide modal if it doesn't exist
    let guideModal = document.getElementById('cookieGuideModal');
    if (!guideModal) {
        guideModal = document.createElement('div');
        guideModal.className = 'modal';
        guideModal.id = 'cookieGuideModal';
        guideModal.innerHTML = `
            <button class="modal-close">&times;</button>
            <h2>🍪 How to Upload Cookies</h2>
            <div class="modal-content">
                <p>Some sites (like YouTube) require your browser cookies to download age-restricted or private content.</p>
                <p><strong>Step 1:</strong> Install a browser extension to export cookies:</p>
                <ul style="margin: 0.5rem 0 1rem 1.2rem; line-height: 1.8;">
                    <li><strong>Chrome:</strong> <a href="https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc" target="_blank" style="color: var(--accent);">Get cookies.txt LOCALLY</a></li>
                    <li><strong>Firefox:</strong> <a href="https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/" target="_blank" style="color: var(--accent);">cookies.txt</a></li>
                </ul>
                <p><strong>Step 2:</strong> Go to <a href="https://youtube.com" target="_blank" style="color: var(--accent);">youtube.com</a> and make sure you're <strong>logged in</strong>.</p>
                <p><strong>Step 3:</strong> Click the extension icon and export/download the <code>cookies.txt</code> file.</p>
                <p><strong>Step 4:</strong> Click the button below to upload it here.</p>
                <button class="btn-cookie" id="btnCookieUploadGuide" style="margin-top: 1rem; padding: 0.6rem 1.5rem; font-size: 0.95rem;">📁 Upload cookies.txt</button>
                <p style="margin-top: 1rem; opacity: 0.6; font-size: 0.8rem;">Your cookies are stored only on the server and never shared. They expire when the server restarts.</p>
            </div>
        `;
        overlay.appendChild(guideModal);
        guideModal.querySelector('.modal-close').addEventListener('click', () => {
            overlay.classList.remove('active');
            guideModal.classList.remove('active');
        });
        document.getElementById('btnCookieUploadGuide').addEventListener('click', () => {
            cookieFileInput.click();
            overlay.classList.remove('active');
            guideModal.classList.remove('active');
        });
    }
    overlay.classList.add('active');
    guideModal.classList.add('active');
}

cookieFileInput.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    try {
        const res = await fetch(`${API}/api/upload-cookies`, { method: 'POST', body: fd });
        const data = await res.json();
        if (data.success) { toast('Cookies uploaded!', 'success'); checkStatus(); }
        else toast('Upload failed', 'error');
    } catch { toast('Upload failed', 'error'); }
    cookieFileInput.value = '';
});

// ─── Theme Toggle ───
const currentTheme = localStorage.getItem('theme');

if (currentTheme === 'dark' || (!currentTheme && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    document.body.classList.add('dark-theme');
}

document.querySelectorAll('.theme-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
        document.body.classList.toggle('dark-theme');
        const isDark = document.body.classList.contains('dark-theme');
        localStorage.setItem('theme', isDark ? 'dark' : 'light');
    });
});

// ─── Sidebar ───
const sidebar = document.getElementById('sidebar');
const sidebarOverlay = document.getElementById('sidebarOverlay');
const hamburgerBtn = document.getElementById('hamburgerBtn');
const sidebarClose = document.getElementById('sidebarClose');

function openSidebar() {
    sidebar.classList.add('active');
    sidebarOverlay.classList.add('active');
}
function closeSidebar() {
    sidebar.classList.remove('active');
    sidebarOverlay.classList.remove('active');
}

hamburgerBtn?.addEventListener('click', openSidebar);
sidebarClose?.addEventListener('click', closeSidebar);
sidebarOverlay?.addEventListener('click', closeSidebar);

// ─── FAQ Accordion ───
const faqItems = document.querySelectorAll('.faq-item');
faqItems.forEach(item => {
    const q = item.querySelector('.faq-q');
    q.addEventListener('click', () => {
        const isActive = item.classList.contains('active');
        // Close all
        faqItems.forEach(i => i.classList.remove('active'));
        // Toggle clicked
        if (!isActive) item.classList.add('active');
    });
});

// ─── Modals ───
const modalOverlay = document.getElementById('modalOverlay');
const privacyModal = document.getElementById('privacyModal');
const termsModal = document.getElementById('termsModal');

function openModal(modal) {
    modalOverlay.classList.add('active');
    modal.classList.add('active');
}
function closeModals() {
    modalOverlay.classList.remove('active');
    document.querySelectorAll('.modal').forEach(m => m.classList.remove('active'));
}

document.getElementById('linkPrivacy')?.addEventListener('click', (e) => { e.preventDefault(); openModal(privacyModal); });
document.getElementById('linkTerms')?.addEventListener('click', (e) => { e.preventDefault(); openModal(termsModal); });
document.getElementById('linkPrivacySidebar')?.addEventListener('click', (e) => { e.preventDefault(); openModal(privacyModal); closeSidebar(); });
document.getElementById('linkTermsSidebar')?.addEventListener('click', (e) => { e.preventDefault(); openModal(termsModal); closeSidebar(); });
modalOverlay?.addEventListener('click', (e) => { if (e.target === modalOverlay) closeModals(); });
document.querySelectorAll('.modal-close').forEach(btn => btn.addEventListener('click', closeModals));

window.addEventListener('load', () => {
    checkStatus();
    loadHistory();
});
