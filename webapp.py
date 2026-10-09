"""Web server for the Learn-X Telegram Mini App.

Runs an aiohttp server inside the same asyncio event loop as the Pyrogram
bot so a single Heroku `web` dyno serves both the bot and the mini app.

Endpoints:
- GET  /miniapp            the mini app HTML (search, tutor browse, download)
- POST /api/visit          logs "Mini App Visited" to the log group
- GET  /api/verify_sub     banned check + channel/group/private membership
- GET  /api/search         search stored PDFs
- GET  /api/tutors         configured tutor groups
- GET  /api/tutor-papers   papers belonging to one tutor
- POST /api/download       gate checks + watermarked delivery to the user
- GET  /api/check_admin    admin check for the mini app admin mode
- POST /api/rename_file    admin: rename a stored PDF
- POST /api/delete_file    admin: delete a stored PDF
"""

import html
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

log = logging.getLogger("learnx.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Same user visiting again within this window is not re-logged (page reloads).
VISIT_DEDUPE_SECONDS = 30
_last_visit = {}

# ================= TELEGRAM MINI APP HTML =================

MINIAPP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Learn-X - Past Papers</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css">
    <style>
        :root {
            --tg-bg: #f5f7fa;
            --tg-accent: #2563eb;
            --tg-card: #ffffff;
        }
        body {
            background: var(--tg-bg);
            font-family: 'Segoe UI', sans-serif;
            min-height: 100vh;
            padding-bottom: 20px;
        }
        .app-header {
            background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%);
            color: white;
            padding: 18px 16px 14px;
            text-align: center;
        }
        .app-header h1 {
            font-size: 1.3rem;
            font-weight: 700;
            margin: 0;
        }
        .app-header p {
            font-size: 0.8rem;
            margin: 4px 0 0;
            opacity: 0.85;
        }
        .admin-badge {
            background: rgba(255,255,255,0.2);
            border-radius: 6px;
            padding: 2px 10px;
            font-size: 0.7rem;
            font-weight: 700;
            margin-top: 5px;
            display: none;
            letter-spacing: 0.05em;
        }
        .search-section {
            padding: 14px 16px;
        }
        .search-bar {
            border-radius: 12px;
            border: 2px solid #e2e8f0;
            padding: 10px 16px;
            font-size: 0.95rem;
            transition: border-color 0.2s;
        }
        .search-bar:focus {
            border-color: var(--tg-accent);
            box-shadow: 0 0 0 3px rgba(37,99,235,0.12);
            outline: none;
        }
        .search-btn {
            border-radius: 12px;
            background: var(--tg-accent);
            border: none;
            padding: 10px 16px;
            color: white;
            font-weight: 600;
        }
        .section-title {
            font-size: 0.85rem;
            font-weight: 700;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            padding: 8px 16px 4px;
        }
        .tutors-grid {
            display: flex;
            gap: 12px;
            padding: 8px 16px 12px;
            justify-content: center;
            flex-wrap: wrap;
        }
        .tutor-btn {
            display: flex;
            flex-direction: column;
            align-items: center;
            cursor: pointer;
            border: none;
            background: transparent;
            padding: 0;
            flex: 0 0 calc(33% - 10px);
            max-width: 110px;
        }
        .tutor-btn:active .tutor-img-wrap {
            transform: scale(0.95);
        }
        .tutor-img-wrap {
            width: 100%;
            aspect-ratio: 1 / 1;
            border-radius: 12px;
            overflow: hidden;
            border: 3px solid transparent;
            background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%);
            transition: border-color 0.2s, transform 0.15s;
            box-shadow: 0 2px 8px rgba(0,0,0,0.10);
        }
        .tutor-btn.active .tutor-img-wrap,
        .tutor-btn:hover .tutor-img-wrap {
            border-color: #93c5fd;
            box-shadow: 0 4px 14px rgba(37,99,235,0.35);
        }
        .tutor-initials {
            width: 100%;
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.5rem;
            font-weight: 700;
            color: white;
            user-select: none;
        }
        .tutor-name {
            margin-top: 6px;
            font-size: 0.72rem;
            font-weight: 600;
            color: #1e3a8a;
            text-align: center;
            line-height: 1.3;
        }
        .results-section {
            padding: 0 16px;
        }
        .result-card {
            background: var(--tg-card);
            border-radius: 12px;
            padding: 12px 14px;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 1px 4px rgba(0,0,0,0.08);
            border: 1px solid #e2e8f0;
        }
        .result-name {
            font-size: 0.88rem;
            font-weight: 500;
            color: #1e293b;
            flex: 1;
            margin-right: 10px;
            word-break: break-word;
        }
        .download-btn {
            background: var(--tg-accent);
            color: white;
            border: none;
            border-radius: 8px;
            padding: 6px 12px;
            font-size: 0.8rem;
            font-weight: 600;
            white-space: nowrap;
            cursor: pointer;
            transition: background 0.2s;
        }
        .download-btn:hover {
            background: #1d4ed8;
        }
        .delete-btn {
            background: #ef4444;
            color: white;
            border: none;
            border-radius: 8px;
            padding: 6px 10px;
            font-size: 0.8rem;
            font-weight: 600;
            white-space: nowrap;
            cursor: pointer;
            margin-left: 6px;
        }
        .empty-state {
            text-align: center;
            padding: 30px 20px;
            color: #94a3b8;
        }
        .empty-state i {
            font-size: 2.5rem;
            display: block;
            margin-bottom: 8px;
        }
        .loading-spinner {
            display: none;
            text-align: center;
            padding: 20px;
        }
        .toast-msg {
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            background: #1e293b;
            color: white;
            padding: 10px 20px;
            border-radius: 20px;
            font-size: 0.85rem;
            z-index: 9999;
            display: none;
            white-space: nowrap;
        }
        .sub-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(15,23,42,0.96);
            z-index: 99999;
            display: none;
            align-items: center;
            justify-content: center;
            flex-direction: column;
            text-align: center;
            padding: 24px;
            color: white;
        }
        .sub-overlay h2 {
            font-size: 1.3rem;
            font-weight: 700;
            margin-bottom: 10px;
        }
        .sub-overlay p {
            font-size: 0.9rem;
            opacity: 0.85;
            margin-bottom: 20px;
        }
        .sub-overlay-btn {
            display: inline-block;
            background: #2563eb;
            color: white;
            border-radius: 10px;
            padding: 10px 22px;
            font-weight: 600;
            text-decoration: none;
            margin: 5px;
        }
    </style>
</head>
<body>
    <div class="app-header">
        <h1>🎓 Learn-X</h1>
        <p>Past papers &amp; tutor collections</p>
        <span class="admin-badge" id="adminBadge">ADMIN MODE</span>
    </div>

    <div class="search-section input-group">
        <input type="text" class="form-control search-bar" id="searchInput" placeholder="Search past papers...">
        <button class="btn search-btn" type="button" onclick="doSearch()"><i class="bi bi-search"></i></button>
    </div>

    <div class="section-title">Tutors</div>
    <div class="tutors-grid" id="tutorsGrid"></div>

    <div class="section-title" id="discussionsTitle" style="display:none;">Discussions</div>
    <div class="tutors-grid" id="discussionsGrid"></div>

    <div class="section-title" id="resultsTitle" style="display:none;">Results</div>
    <div class="loading-spinner" id="loadingSpinner">
        <div class="spinner-border text-primary" role="status"></div>
    </div>
    <div class="results-section" id="resultsContainer">
        <div class="empty-state">
            <i class="bi bi-search"></i>
            <p>Search for papers above or tap a tutor to browse their papers.</p>
        </div>
    </div>

    <div id="subOverlay" class="sub-overlay">
        <div style="font-size:2.5rem;margin-bottom:12px;">🔒</div>
        <h2>Access Restricted</h2>
        <p>You must join our official Channel &amp; Group to use Learn-X.</p>
        <div id="subOverlayLinks"></div>
        <div style="margin-top:18px;font-size:0.8rem;opacity:0.6;">After joining, reload the app.</div>
    </div>

    <div class="toast-msg" id="toastMsg"></div>

    <script>
        const tg = window.Telegram && window.Telegram.WebApp;
        if (tg) {
            tg.ready();
            tg.expand();
            document.body.style.background = tg.themeParams.bg_color || '#f5f7fa';
        }

        function getDeviceInfo() {
            return { platform: (tg && tg.platform) ? tg.platform : 'unknown', userAgent: navigator.userAgent };
        }

        function currentTelegramUser() {
            return (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) ? tg.initDataUnsafe.user : null;
        }

        function logVisit() {
            const user = currentTelegramUser();
            if (!user) return;
            fetch('/api/visit', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    user_id: user.id,
                    username: user.username || '',
                    first_name: user.first_name || '',
                    last_name: user.last_name || '',
                    device: getDeviceInfo()
                })
            }).catch(function() {});
        }

        (function checkSubscription() {
            const user = currentTelegramUser();
            if (!user) return;
            fetch('/api/verify_sub?user_id=' + encodeURIComponent(user.id))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    const overlay = document.getElementById('subOverlay');
                    if (data.banned) {
                        overlay.style.display = 'flex';
                        overlay.innerHTML = '<div style="font-size:2.5rem;margin-bottom:12px;">🚫</div>' +
                                            '<h2>Access Denied</h2>' +
                                            '<p>You have been permanently banned from using Learn-X.</p>';
                        return;
                    }
                    if (data.subscribed) {
                        overlay.style.display = 'none';
                        return;
                    }
                    overlay.style.display = 'flex';
                    const linksDiv = document.getElementById('subOverlayLinks');
                    linksDiv.innerHTML = '';
                    if (data.private_required) {
                        overlay.innerHTML = '<div style="font-size:2.5rem;margin-bottom:12px;">🔒</div>' +
                                            '<h2>Private access required</h2>' +
                                            '<p>You do not have permission to access these PDFs.</p>';
                        return;
                    }
                    if (data.channel_url) {
                        const a = document.createElement('a');
                        a.className = 'sub-overlay-btn';
                        a.href = data.channel_url;
                        a.target = '_blank';
                        a.rel = 'noopener noreferrer';
                        a.textContent = '📢 Join Channel';
                        linksDiv.appendChild(a);
                    }
                    if (data.group_url) {
                        const a = document.createElement('a');
                        a.className = 'sub-overlay-btn';
                        a.href = data.group_url;
                        a.target = '_blank';
                        a.rel = 'noopener noreferrer';
                        a.textContent = '👥 Join Group';
                        linksDiv.appendChild(a);
                    }
                })
                .catch(function() {});
        })();

        let currentKey = null;
        let currentTutorLabel = null;
        var isAdmin = false;

        function showToast(msg, dur) {
            const t = document.getElementById('toastMsg');
            t.textContent = msg;
            t.style.display = 'block';
            setTimeout(() => { t.style.display = 'none'; }, dur || 2000);
        }

        function setLoading(show) {
            document.getElementById('loadingSpinner').style.display = show ? 'block' : 'none';
        }

        function escapeHtml(str) {
            return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        }
        function escapeAttr(str) {
            return escapeHtml(str).replace(/'/g, '&#39;');
        }
        function initials(name) {
            const parts = (name || '').trim().split(/\\s+/).filter(Boolean);
            if (!parts.length) return 'LX';
            if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
            return (parts[0][0] + parts[1][0]).toUpperCase();
        }

        function renderResults(files, emptyMsg) {
            const container = document.getElementById('resultsContainer');
            const title = document.getElementById('resultsTitle');
            if (!files || files.length === 0) {
                title.style.display = 'none';
                container.innerHTML = '<div class="empty-state"><i class="bi bi-inbox"></i><p>' + (emptyMsg || 'No papers found.') + '</p></div>';
                return;
            }
            title.style.display = 'block';
            title.textContent = 'Results';
            container.innerHTML = files.map(function(f) {
                var renameBtn = isAdmin ? '<button class="delete-btn" style="background: #f59e0b; margin-left: 6px;" onclick=\\'renameFile(' + f.id + ')\\'><i class="bi bi-pencil-square"></i></button>' : '';
                var deleteBtn = isAdmin ? '<button class="delete-btn" onclick=\\'deleteFile(' + f.id + ')\\'><i class="bi bi-trash"></i></button>' : '';
                return '<div class="result-card" id="card-' + f.id + '">'
                    + '<span class="result-name"><i class="bi bi-file-earmark-pdf-fill text-danger me-2"></i>' + escapeHtml(f.file_name) + '</span>'
                    + '<button class="download-btn" onclick=\\'downloadFile(' + f.id + ', ' + JSON.stringify(f.file_name).replace(/'/g, "&#39;") + ')\\'><i class="bi bi-download"></i> Get</button>'
                    + renameBtn
                    + deleteBtn
                    + '</div>';
            }).join('');
        }

        function loadTutorButtons() {
            fetch('/api/tutors')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    const grid = document.getElementById('tutorsGrid');
                    const tutors = (data && data.tutors) ? data.tutors : [];
                    if (!tutors.length) {
                        grid.innerHTML = '<div class="empty-state" style="padding:10px;"><p>No tutors added yet.</p></div>';
                        return;
                    }
                    grid.innerHTML = tutors.map(function(t, idx) {
                        const id = 'tutor-btn-' + idx;
                        const key = t.key || '';
                        const name = t.display_name || key;
                        const inner = t.image_url
                            ? '<img src="' + escapeAttr(t.image_url) + '" alt="' + escapeAttr(name) + '" onerror="this.style.display=\\'none\\'; this.nextElementSibling.style.display=\\'flex\\';">'
                              + '<div class="tutor-initials" style="display:none;">' + escapeHtml(initials(name)) + '</div>'
                            : '<div class="tutor-initials">' + escapeHtml(initials(name)) + '</div>';
                        return "<button class='tutor-btn' id='" + id + "' onclick='loadByTutor(" + JSON.stringify(key) + ", " + JSON.stringify(id) + ", " + JSON.stringify(name) + ")'>"
                            + '<div class="tutor-img-wrap">' + inner + '</div>'
                            + '<span class="tutor-name">' + escapeHtml(name) + '</span>'
                            + '</button>';
                    }).join('');
                })
                .catch(function() { showToast('Failed to load tutor buttons.'); });
        }

        function doSearch() {
            const q = document.getElementById('searchInput').value.trim();
            if (!q) { showToast('Please enter a search keyword.'); return; }
            document.querySelectorAll('.tutor-btn').forEach(function(b) { b.classList.remove('active'); });
            currentKey = null;
            currentTutorLabel = null;
            setLoading(true);
            fetch('/api/search?q=' + encodeURIComponent(q))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    setLoading(false);
                    renderResults(data.files, 'No papers found for "' + escapeHtml(q) + '".');
                })
                .catch(function() { setLoading(false); showToast('Search failed. Please try again.'); });
        }

        function loadByTutor(key, btnId, displayName) {
            if (!key) {
                showToast('Tutor key missing.');
                return;
            }
            if (currentKey === key) {
                currentKey = null;
                currentTutorLabel = null;
                document.getElementById(btnId).classList.remove('active');
                document.getElementById('resultsTitle').style.display = 'none';
                document.getElementById('resultsContainer').innerHTML = '<div class="empty-state"><i class="bi bi-search"></i><p>Search for papers above or tap a tutor to browse their papers.</p></div>';
                return;
            }
            currentKey = key;
            currentTutorLabel = displayName || key;
            document.querySelectorAll('.tutor-btn').forEach(function(b) { b.classList.remove('active'); });
            document.getElementById(btnId).classList.add('active');
            document.getElementById('searchInput').value = '';
            setLoading(true);
            fetch('/api/tutor-papers?key=' + encodeURIComponent(key))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    setLoading(false);
                    renderResults(data.files, 'No papers found for ' + (currentTutorLabel || key) + '.');
                })
                .catch(function() { setLoading(false); showToast('Failed to load papers. Please try again.'); });
        }

        function downloadFile(fileId, fileName) {
            const user = currentTelegramUser();
            if (!user) {
                showToast('⚠️ Open this app from Telegram to download files.', 3000);
                return;
            }
            showToast('Sending to your chat...', 3000);
            fetch('/api/download', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    file_id: fileId,
                    user_id: user.id,
                    file_name: fileName,
                    username: user.username || "",
                    first_name: user.first_name || "",
                    last_name: user.last_name || "",
                    device: getDeviceInfo()
                })
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.ok) {
                    showToast('✅ Sent to your Telegram chat!', 3000);
                } else if (data.error === 'banned') {
                    showToast('🚫 You have been banned from using Learn-X.', 4000);
                } else if (data.error === 'subscription_required') {
                    showToast('⚠️ Please join our required Channel/Group to download files!', 4000);
                    document.getElementById('subOverlay').style.display = 'flex';
                } else if (data.error === 'tutor_group_required') {
                    showToast('🔒 Join the ' + (data.tutor || 'tutor') + ' group to get this paper.', 4000);
                    if (data.invite_url) {
                        if (tg && tg.openTelegramLink) { tg.openTelegramLink(data.invite_url); }
                        else { window.open(data.invite_url, '_blank'); }
                    }
                } else if (data.error === 'not_found') {
                    showToast('File not found.', 3000);
                } else {
                    showToast('❌ Failed to send. Please try again.', 3000);
                }
            })
            .catch(function() { showToast('❌ Network error. Please try again.', 3000); });
        }

        function renameFile(fileId) {
            if (!isAdmin) return;
            var user = currentTelegramUser();
            if (!user) { showToast('Admin action requires Telegram.', 3000); return; }
            var card = document.getElementById('card-' + fileId);
            var nameSpan = card ? card.querySelector('.result-name') : null;
            var oldName = nameSpan ? nameSpan.textContent.trim() : '';
            var newName = prompt("Enter new file name:", oldName);
            if (newName !== null && newName.trim() !== "" && newName.trim() !== oldName) {
                fetch('/api/rename_file', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({file_id: fileId, new_name: newName.trim(), user_id: user.id})
                })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.ok) {
                        showToast('✅ File renamed successfully.', 2500);
                        if (nameSpan) {
                            nameSpan.innerHTML = '<i class="bi bi-file-earmark-pdf-fill text-danger me-2"></i>' + escapeHtml(data.file_name);
                        }
                    } else {
                        showToast('❌ ' + (data.error || 'Failed to rename.'), 3000);
                    }
                })
                .catch(function() { showToast('❌ Network error.', 3000); });
            }
        }

        function deleteFile(fileId) {
            if (!isAdmin) return;
            var user = currentTelegramUser();
            if (!user) { showToast('Admin action requires Telegram.', 3000); return; }
            var card = document.getElementById('card-' + fileId);
            var nameSpan = card ? card.querySelector('.result-name') : null;
            var fileName = nameSpan ? nameSpan.textContent.trim() : 'this file';
            var doDelete = function() {
                fetch('/api/delete_file', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({file_id: fileId, user_id: user.id})
                })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.ok) {
                        var el = document.getElementById('card-' + fileId);
                        if (el) el.remove();
                        showToast('✅ File deleted.', 2500);
                    } else {
                        showToast('❌ ' + (data.error || 'Failed to delete.'), 3000);
                    }
                })
                .catch(function() { showToast('❌ Network error.', 3000); });
            };
            if (tg && tg.showConfirm) {
                tg.showConfirm('Are you sure you want to delete "' + fileName + '"?', function(ok) {
                    if (ok) doDelete();
                });
            } else {
                if (confirm('Are you sure you want to delete "' + fileName + '"?')) doDelete();
            }
        }

        document.getElementById('searchInput').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') doSearch();
        });

        function checkAdminMode() {
            const user = currentTelegramUser();
            if (!user) return;
            fetch('/api/check_admin?user_id=' + encodeURIComponent(user.id))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.is_admin) {
                        isAdmin = true;
                        document.getElementById('adminBadge').style.display = 'inline-block';
                    }
                })
                .catch(function() {});
        }

        function sendDiscussion(key) {
            const user = currentTelegramUser();
            if (!user) { showToast('⚠️ Open this app from Telegram to request discussions.', 3000); return; }
            showToast('Sending ' + key.toUpperCase() + ' discussions...', 3000);
            fetch('/api/discussions/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({user_id: user.id, tutor: key})
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.ok) { showToast('✅ Discussions sent to your Telegram chat!', 3000); }
                else { showToast('❌ ' + (data.error || 'Failed to send discussions.'), 4000); }
            })
            .catch(function() { showToast('❌ Network error. Please try again.', 3000); });
        }

        function loadDiscussionButtons() {
            fetch('/api/tutors')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    const grid = document.getElementById('discussionsGrid');
                    const tutors = (data && data.tutors) ? data.tutors.filter(function(t) { return t.discussions; }) : [];
                    if (!tutors.length) { document.getElementById('discussionsTitle').style.display = 'none'; return; }
                    grid.innerHTML = tutors.map(function(t) {
                        const name = t.display_name || t.key;
                        return "<button class='tutor-btn' style='flex: 0 0 auto; max-width: none;' onclick='sendDiscussion(" + JSON.stringify(t.key) + ")'>"
                            + '<div class="tutor-img-wrap" style="width:54px;"><div class="tutor-initials" style="font-size:1rem;">' + escapeHtml(t.key.toUpperCase()) + '</div></div>'
                            + '<span class="tutor-name">' + escapeHtml(name) + '</span>'
                            + '</button>';
                    }).join('');
                })
                .catch(function() {});
        }

        loadTutorButtons();
        loadDiscussionButtons();
        checkAdminMode();
        logVisit();
    </script>
</body>
</html>"""

# ================= HELPERS =================


def _client_ip(request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


def _user_from_payload(data: dict):
    user_id = data.get("user_id") or data.get("id")
    if user_id is None:
        return None
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    return SimpleNamespace(
        id=user_id,
        username=(data.get("username") or "").strip() or None,
        first_name=(data.get("first_name") or "").strip() or "",
        last_name=(data.get("last_name") or "").strip() or "",
    )


async def _json_body(request) -> dict:
    """Parse a JSON body, returning {} instead of raising on bad input."""
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _json(data: dict, status: int = 200):
    return web.json_response(data, status=status)


# ================= ROUTES =================


async def index(request):
    return web.Response(text="Learn-X PDF bot is running.", content_type="text/plain")


async def miniapp(request):
    return web.Response(text=MINIAPP_HTML, content_type="text/html")


async def api_visit(request):
    """Notify the log group whenever someone opens the mini app."""
    import app as bot

    data = await _json_body(request)
    user = _user_from_payload(data)
    if user is None:
        return _json({"ok": False}, 400)

    try:
        await bot.sync_user(user)
    except Exception:
        pass

    now = time.time()
    last = _last_visit.get(user.id, 0)
    if now - last < VISIT_DEDUPE_SECONDS:
        return _json({"ok": True, "deduped": True})
    _last_visit[user.id] = now
    if len(_last_visit) > 5000:  # keep the dedupe cache bounded
        cutoff = now - VISIT_DEDUPE_SECONDS
        _last_visit.update({k: v for k, v in _last_visit.items() if v > cutoff})

    device = data.get("device") or {}
    ip = _client_ip(request)
    platform = str(device.get("platform") or "unknown")[:64]
    browser = str(device.get("userAgent") or "unknown")[:400]

    full_name = " ".join(part for part in (user.first_name, user.last_name) if part) or "Unknown"
    username_display = f"@{user.username}" if user.username else "No username"

    try:
        await bot.send_log(
            "👀 <b>Mini App Visited</b>\n"
            f"User: {html.escape(full_name)}\n"
            f"Username: {html.escape(username_display)}\n"
            f"ID: <code>{user.id}</code>\n"
            "🌐 <b>Device &amp; Network</b>\n"
            f"IP: <code>{html.escape(ip)}</code>\n"
            f"Platform: <code>{html.escape(platform)}</code>\n"
            f"Browser: <code>{html.escape(browser)}</code>"
        )
    except Exception:
        log.exception("Could not send mini app visit log")
    try:
        bot.db.log_visit(user, ip, platform, browser)
    except Exception:
        log.exception("Could not store mini app visit")
    return _json({"ok": True})


async def api_discussions_send(request):
    """Send a tutor's discussion materials to the user (mini app button)."""
    import app as bot

    data = await _json_body(request)
    key = str(data.get("tutor", "")).strip().lower()
    user = _user_from_payload(data)
    if user is None or not key:
        return _json({"ok": False, "error": "Invalid request"}, 400)
    if key not in bot.settings.discussions:
        return _json(
            {"ok": False, "error": "Discussion materials are not configured for this tutor."},
            404,
        )
    try:
        await bot.sync_user(user)
    except Exception:
        pass
    if bot.db.is_banned(user.id):
        return _json({"ok": False, "error": "You are banned from using Learn-X."})
    status = await bot.access_status(user.id)
    if not all(status.values()):
        return _json(
            {"ok": False, "error": "Please complete verification first."}, 403
        )
    ok, err = await bot.send_discussion_messages(user.id, key)
    if ok:
        try:
            await bot.send_log(
                "💬 <b>Discussions sent (Mini App)</b>\n"
                f"<b>User:</b> {bot.mention(user)}\n"
                f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
                f"<b>Tutor:</b> <code>{html.escape(key)}</code>"
            )
        except Exception:
            pass
        return _json({"ok": True})
    return _json({"ok": False, "error": err}, 500)


async def api_verify_sub(request):
    import app as bot

    user_id = request.query.get("user_id")
    if not user_id:
        return _json({"subscribed": False, "error": "Missing user_id"}, 400)
    try:
        uid = int(user_id)
    except ValueError:
        return _json({"subscribed": False, "error": "Invalid user_id"}, 400)

    if bot.db.is_banned(uid):
        return _json({"subscribed": False, "banned": True})

    status = await bot.access_status(uid)
    result = {"subscribed": all(status.values())}
    if not status["main"]:
        result["channel_url"] = bot.settings.main_channel_url
    if not status["public"]:
        result["group_url"] = bot.settings.public_group_url
    if status["main"] and status["public"] and not status["private"]:
        result["private_required"] = True
    return _json(result)


async def api_search(request):
    import app as bot

    query = (request.query.get("q") or "").strip()
    if not query:
        return _json({"files": [], "error": "No query provided"})
    _, rows = bot.db.search_files(bot.normalize_query(query), 20)
    files = [{"id": row["id"], "file_name": row["display_name"]} for row in rows]
    return _json({"files": files})


async def api_tutors(request):
    import app as bot

    tutors = [
        {
            "key": t["key"],
            "display_name": t["display_name"],
            "image_url": (
                f"/static/{t['key']}.jpg"
                if (STATIC_DIR / f"{t['key']}.jpg").is_file()
                else ""
            ),
            "discussions": t["key"] in bot.settings.discussions,
        }
        for t in bot.db.list_tutors()
    ]
    return _json({"tutors": tutors})


async def api_tutor_papers(request):
    import app as bot

    key = (request.query.get("key") or "").strip().lower()
    if not key:
        return _json({"files": [], "error": "Invalid key"})
    rows = bot.db.files_for_tutor(key)
    files = [{"id": row["id"], "file_name": row["display_name"]} for row in rows]
    return _json({"files": files})


async def api_download(request):
    """Run the full gate checks and deliver a watermarked PDF via the bot."""
    import app as bot

    data = await _json_body(request)
    user = _user_from_payload(data)
    if user is None or not str(data.get("file_id", "")).strip():
        return _json({"ok": False, "error": "Missing file_id or user_id"}, 400)

    try:
        await bot.sync_user(user)
    except Exception:
        pass

    if bot.db.is_banned(user.id):
        return _json({"ok": False, "error": "banned"})

    status = await bot.access_status(user.id)
    if not all(status.values()):
        return _json({"ok": False, "error": "subscription_required"})

    try:
        file_id = int(str(data.get("file_id")).strip())
    except ValueError:
        return _json({"ok": False, "error": "not_found"})
    file_record = bot.db.get_file(file_id)
    if not file_record:
        return _json({"ok": False, "error": "not_found"})

    tutor = bot.db.tutor_for_file(file_record)
    if tutor and not await bot.membership(tutor["group_ref"], user.id):
        try:
            await bot.send_log(
                "🔐 <b>Tutor group membership required (Mini App)</b>\n"
                f"<b>User:</b> {bot.mention(user)}\n"
                f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
                f"<b>Tutor group:</b> <code>{html.escape(tutor['display_name'])}</code>\n"
                f"<b>File:</b> <code>{html.escape(file_record['display_name'])}</code>\n"
                "❌ <b>Tutor group:</b> not a member"
            )
        except Exception:
            pass
        return _json(
            {
                "ok": False,
                "error": "tutor_group_required",
                "tutor": tutor["display_name"],
                "invite_url": tutor.get("invite_url", ""),
            }
        )

    query_label = str(data.get("query") or "").strip() or (
        data.get("file_name") or file_record["display_name"]
    )
    try:
        await bot.deliver_pdf_to_user(
            user, file_record, query_label, source="Mini App"
        )
    except Exception:
        log.exception("Mini app delivery failed")
        return _json({"ok": False, "error": "delivery_failed"})
    return _json({"ok": True})


async def api_check_admin(request):
    import app as bot

    user_id = request.query.get("user_id")
    try:
        return _json({"is_admin": bot.db.is_admin(int(user_id))})
    except (TypeError, ValueError):
        return _json({"is_admin": False})


async def api_rename_file(request):
    import app as bot

    data = await _json_body(request)
    try:
        user_id = int(data.get("user_id"))
        file_id = int(str(data.get("file_id", "")).strip())
    except (TypeError, ValueError):
        return _json({"ok": False, "error": "Missing parameters"}, 400)
    new_name = (data.get("new_name") or "").strip()
    if not new_name:
        return _json({"ok": False, "error": "Missing parameters"}, 400)
    if not bot.db.is_admin(user_id):
        return _json({"ok": False, "error": "Unauthorized"}, 403)

    record = bot.db.get_file(file_id)
    if not record:
        return _json({"ok": False, "error": "File not found"}, 404)
    try:
        count = bot.db.rename_file(
            record["search_name"], new_name, bot.normalize_query(new_name)
        )
    except Exception:
        return _json({"ok": False, "error": "A file with the new name already exists."})
    if not count:
        return _json({"ok": False, "error": "File not found"}, 404)
    return _json({"ok": True, "file_name": new_name})


async def api_delete_file(request):
    import app as bot

    data = await _json_body(request)
    try:
        user_id = int(data.get("user_id"))
        file_id = int(str(data.get("file_id", "")).strip())
    except (TypeError, ValueError):
        return _json({"ok": False, "error": "Missing parameters"}, 400)
    if not bot.db.is_admin(user_id):
        return _json({"ok": False, "error": "Unauthorized"}, 403)

    record = bot.db.get_file(file_id)
    if not record:
        return _json({"ok": False, "error": "File not found"}, 404)
    count = bot.db.delete_file(record["search_name"])
    if not count:
        return _json({"ok": False, "error": "File not found"}, 404)
    return _json({"ok": True})


# ================= WEB ADMIN PANEL =================

_PANEL_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css">
</head>
<body class="bg-light">
<nav class="navbar navbar-dark bg-dark mb-4 shadow">
    <div class="container">
        <span class="navbar-brand mb-0 h1"><i class="bi bi-robot"></i> {brand}</span>
        <span>
            <a class="btn btn-sm btn-outline-light me-2" href="/panel?key={key}">Dashboard</a>
            <a class="btn btn-sm btn-outline-light" href="/messages?key={key}">User Messages</a>
        </span>
    </div>
</nav>
<div class="container">
"""
_PANEL_FOOT = """
</div>
<div class="container text-center text-muted small py-4">Powered by Learn-X PDF Bot</div>
</body>
</html>"""


def _panel_authorized(request) -> bool:
    import app as bot

    password = bot.settings.panel_password
    if not password:
        return False
    return secrets.compare_digest(request.query.get("key", ""), password)


def _panel_disabled_page() -> web.Response:
    return web.Response(
        text=(
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Panel disabled</title></head><body style='font-family:sans-serif;"
            "padding:40px;text-align:center'><h2>🔒 Admin panel disabled</h2>"
            "<p>Set the <code>PANEL_PASSWORD</code> config variable to enable "
 "this panel, then open <code>/panel?key=YOUR_PASSWORD</code>.</p>"
            "</body></html>"
        ),
        content_type="text/html",
        status=403,
    )


def _panel_unauthorized_page() -> web.Response:
    return web.Response(
        text=(
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Unauthorized</title></head><body style='font-family:sans-serif;"
            "padding:40px;text-align:center'><h2>🚫 Unauthorized</h2>"
            "<p>Open <code>/panel?key=YOUR_PANEL_PASSWORD</code>.</p></body></html>"
        ),
        content_type="text/html",
        status=401,
    )


def _stat_card(icon: str, label: str, value, bg: str) -> str:
    return f"""
                <div class="col-md-4 mb-3">
                    <div class="card text-white {bg} h-100 shadow-sm">
                        <div class="card-body text-center">
                            <h5 class="card-title"><i class="bi bi-{icon}"></i> {label}</h5>
                            <h1 class="display-4 fw-bold">{value}</h1>
                        </div>
                    </div>
                </div>"""


async def panel_dashboard(request):
    import app as bot

    if not bot.settings.panel_password:
        return _panel_disabled_page()
    if not _panel_authorized(request):
        return _panel_unauthorized_page()

    s = bot.db.stats()
    visits = bot.db.recent_visits(20)
    deliveries = bot.db.recent_deliveries(20)
    key = request.query.get("key", "")

    visit_rows = "".join(
        f"<tr><td>{html.escape(v['created_at'])}</td>"
        f"<td>{html.escape((v['first_name'] + ' ' + v['last_name']).strip() or 'Unknown')}"
        f" <small class='text-muted'>({v['user_id']})</small></td>"
        f"<td><code>{html.escape(v['username'] or '—')}</code></td>"
        f"<td><code>{html.escape(v['ip'])}</code></td>"
        f"<td>{html.escape(v['platform'])}</td></tr>"
        for v in visits
    ) or "<tr><td colspan='5' class='text-center text-muted'>No mini app visits yet.</td></tr>"

    delivery_rows = "".join(
        f"<tr><td>{html.escape(d['created_at'])}</td>"
        f"<td>{html.escape((d['first_name'] + ' ' + d['last_name']).strip() or 'Unknown')}"
        f" <small class='text-muted'>({d['user_id']})</small></td>"
        f"<td><code>{html.escape(d['trace_id'])}</code> / <code>{html.escape(d['visible_code'])}</code></td>"
        f"<td><code>{html.escape(d['status'])}</code></td></tr>"
        for d in deliveries
    ) or "<tr><td colspan='4' class='text-center text-muted'>No deliveries yet.</td></tr>"

    body = _PANEL_HEAD.format(title="Learn-X Dashboard", brand="Learn-X Dashboard", key=key)
    body += '<div class="row">'
    body += _stat_card("people-fill", "Total Users", s["users"], "bg-primary")
    body += _stat_card("file-earmark-pdf-fill", "Stored PDFs", s["files"], "bg-success")
    body += _stat_card("send-fill", "Deliveries", f"{s['deliveries']} ({s['successful']} ok)", "bg-warning")
    body += "</div><div class='row'>"
    body += _stat_card("slash-circle-fill", "Banned Users", s["banned"], "bg-danger")
    body += _stat_card("mortarboard-fill", "Tutor Groups", s["tutors"], "bg-info")
    visit_count = len(bot.db.recent_visits(1000))
    body += _stat_card("eye-fill", "Mini App Visits", visit_count, "bg-secondary")
    body += "</div>"
    body += """
<div class="card shadow-sm mb-4"><div class="card-body">
<h5><i class="bi bi-eye-fill"></i> Recent Mini App Visits</h5>
<div class="table-responsive"><table class="table table-striped table-bordered table-hover table-sm">
<thead class="table-dark"><tr><th>Date / Time</th><th>User</th><th>Username</th><th>IP</th><th>Platform</th></tr></thead>
<tbody>""" + visit_rows + """</tbody></table></div></div></div>
<div class="card shadow-sm mb-4"><div class="card-body">
<h5><i class="bi bi-send-fill"></i> Recent Deliveries</h5>
<div class="table-responsive"><table class="table table-striped table-bordered table-hover table-sm">
<thead class="table-dark"><tr><th>Date / Time</th><th>User</th><th>Trace / Code</th><th>Status</th></tr></thead>
<tbody>""" + delivery_rows + """</tbody></table></div></div></div>"""
    body += _PANEL_FOOT
    return web.Response(text=body, content_type="text/html")


async def panel_messages(request):
    import app as bot

    if not bot.settings.panel_password:
        return _panel_disabled_page()
    if not _panel_authorized(request):
        return _panel_unauthorized_page()

    messages = bot.db.recent_messages(200)
    rows = "".join(
        f"<tr><td>{html.escape(m['created_at'])}</td>"
        f"<td>{html.escape((m['first_name'] + ' ' + m['last_name']).strip() or 'Unknown')}"
        f" <small class='text-muted'>({m['user_id']})</small></td>"
        f"<td><code>{html.escape(m['username'] or '—')}</code></td>"
        f"<td>{html.escape(m['message'])}</td></tr>"
        for m in messages
    ) or "<tr><td colspan='4' class='text-center text-muted'>No user messages yet.</td></tr>"

    key = request.query.get("key", "")
    body = _PANEL_HEAD.format(title="User Messages", brand="User Messages", key=key)
    body += """
<div class="card shadow-sm"><div class="card-body">
<h5><i class="bi bi-chat-dots-fill"></i> Recent User Messages &amp; Searches</h5>
<div class="table-responsive"><table class="table table-striped table-bordered table-hover table-sm">
<thead class="table-dark"><tr><th>Date / Time</th><th>User</th><th>Username</th><th>Message</th></tr></thead>
<tbody>""" + rows + """</tbody></table></div></div></div>"""
    body += _PANEL_FOOT
    return web.Response(text=body, content_type="text/html")


# ================= SERVER =================


def make_app() -> web.Application:
    webapp = web.Application()
    webapp.router.add_get("/", index)
    webapp.router.add_get("/miniapp", miniapp)
    webapp.router.add_post("/api/visit", api_visit)
    webapp.router.add_get("/api/verify_sub", api_verify_sub)
    webapp.router.add_get("/api/search", api_search)
    webapp.router.add_get("/api/tutors", api_tutors)
    webapp.router.add_get("/api/tutor-papers", api_tutor_papers)
    webapp.router.add_post("/api/download", api_download)
    webapp.router.add_get("/api/check_admin", api_check_admin)
    webapp.router.add_post("/api/rename_file", api_rename_file)
    webapp.router.add_post("/api/delete_file", api_delete_file)
    webapp.router.add_post("/api/discussions/send", api_discussions_send)
    webapp.router.add_get("/panel", panel_dashboard)
    webapp.router.add_get("/messages", panel_messages)
    if STATIC_DIR.is_dir():
        webapp.router.add_static("/static", STATIC_DIR, show_index=False)
    return webapp


async def start_web_server():
    """Start the aiohttp server inside the bot's event loop (Heroku $PORT)."""
    runner = web.AppRunner(make_app(), access_log=None)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Mini app web server listening on port %s", port)
    return runner
