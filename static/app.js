document.addEventListener("DOMContentLoaded", () => {
    // Current state variables
    let currentMovies = [];
    let currentShows = [];
    let logInterval = null;
    
    // Action state for confirmation modal
    let pendingAction = {
        type: null, // "spektor" or "restore"
        serverType: null,
        itemId: null,
        title: null,
        filePath: null,
        size: null
    };

    // DOM Elements
    const navItems = document.querySelectorAll(".nav-item");
    const tabPanes = document.querySelectorAll(".tab-pane");
    const pageTitle = document.getElementById("page-title");
    const btnRefresh = document.getElementById("btn-refresh");
    const toastContainer = document.getElementById("toast-container");

    // Modal elements
    const modalSeasons = document.getElementById("modal-seasons");
    const modalEpisodes = document.getElementById("modal-episodes");
    const modalConfirm = document.getElementById("modal-confirm");
    
    // -----------------------------------------------------------------------
    // Helper Functions
    // -----------------------------------------------------------------------
    
    function formatBytes(bytes, decimals = 1) {
        if (!bytes || bytes === 0) return "0 Bytes";
        const k = 1024;
        const dm = decimals < 0 ? 0 : decimals;
        const sizes = ["Bytes", "KB", "MB", "GB", "TB"];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
    }

    function showToast(message, type = "success") {
        const toast = document.createElement("div");
        toast.className = `toast ${type}`;
        
        let icon = "fa-check-circle";
        if (type === "error") icon = "fa-times-circle";
        if (type === "warning") icon = "fa-exclamation-circle";
        
        toast.innerHTML = `
            <i class="fa-solid ${icon}"></i>
            <span>${message}</span>
        `;
        toastContainer.appendChild(toast);
        
        // Remove toast after 4s
        setTimeout(() => {
            toast.style.animation = "toastSlideIn 0.3s reverse forwards";
            setTimeout(() => toast.remove(), 300);
        }, 4000);
    }

    function openModal(modal) {
        modal.style.display = "flex";
    }

    function closeModal(modal) {
        modal.style.display = "none";
    }

    // Bind Close Buttons for Modals
    document.querySelectorAll(".close-modal").forEach(btn => {
        btn.addEventListener("click", () => {
            const modalId = btn.getAttribute("data-modal");
            closeModal(document.getElementById(modalId));
        });
    });

    // Close modal on click outside content
    window.addEventListener("click", (e) => {
        if (e.target.classList.contains("modal")) {
            closeModal(e.target);
        }
    });

    // -----------------------------------------------------------------------
    // Tab Switching Routing Logic
    // -----------------------------------------------------------------------
    
    navItems.forEach(item => {
        item.addEventListener("click", (e) => {
            e.preventDefault();
            const tabId = item.getAttribute("data-tab");
            
            // Toggle active menu item
            navItems.forEach(nav => nav.classList.remove("active"));
            item.classList.add("active");
            
            // Toggle active tab pane
            tabPanes.forEach(pane => pane.classList.remove("active"));
            const targetPane = document.getElementById(`tab-${tabId}`);
            if (targetPane) targetPane.classList.add("active");
            
            // Update page title header
            pageTitle.textContent = item.textContent.trim();
            
            // Handle tab specific loading
            handleTabActivation(tabId);
        });
    });

    function handleTabActivation(tabId) {
        // Clear log polling if not on dashboard
        if (tabId !== "dashboard" && logInterval) {
            clearInterval(logInterval);
            logInterval = null;
        }

        switch (tabId) {
            case "dashboard":
                loadDashboardData();
                startLogPolling();
                break;
            case "movies":
                loadMovies();
                break;
            case "shows":
                loadShows();
                break;
            case "settings":
                loadSettings();
                break;
        }
    }

    // Refresh Button Hook
    btnRefresh.addEventListener("click", () => {
        const activeTab = document.querySelector(".nav-item.active").getAttribute("data-tab");
        handleTabActivation(activeTab);
        showToast("Refreshed data from server", "success");
    });

    // -----------------------------------------------------------------------
    // Tab 1: Dashboard Logic
    // -----------------------------------------------------------------------
    
    function loadDashboardData() {
        // Fetch stats
        fetch("/api/stats")
            .then(res => res.json())
            .then(data => {
                document.getElementById("stat-saved").textContent = formatBytes(data.total_saved_bytes);
                document.getElementById("stat-items").textContent = data.total_items;
            })
            .catch(err => {
                console.error("Failed to load stats:", err);
                showToast("Failed to load dashboard statistics", "error");
            });

        // Fetch config to count servers
        fetch("/api/config")
            .then(res => res.json())
            .then(config => {
                const activeCount = (config.servers || []).filter(s => s.enabled).length;
                document.getElementById("stat-servers").textContent = activeCount;
            })
            .catch(err => console.error("Failed to load config:", err));
    }

    function startLogPolling() {
        if (logInterval) clearInterval(logInterval);
        
        function fetchLogs() {
            fetch("/api/logs")
                .then(res => res.json())
                .then(logs => {
                    const consoleEl = document.getElementById("log-console");
                    if (logs.length === 0) {
                        consoleEl.textContent = "No log records found. Start scanning to inspect logs here.";
                        return;
                    }
                    consoleEl.textContent = logs.join("\n");
                    consoleEl.scrollTop = consoleEl.scrollHeight; // Auto scroll
                })
                .catch(err => console.error("Failed to poll logs:", err));
        }

        fetchLogs();
        logInterval = setInterval(fetchLogs, 2000); // Poll every 2s
    }

    // -----------------------------------------------------------------------
    // Tab 2: Movies Logic
    // -----------------------------------------------------------------------
    
    function loadMovies() {
        const grid = document.getElementById("movies-grid");
        grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted);"><i class="fa-solid fa-spinner fa-spin fa-2x"></i><p style="margin-top: 1rem;">Scanning movies library...</p></div>';

        fetch("/api/movies")
            .then(res => res.json())
            .then(movies => {
                currentMovies = movies;
                renderMovies(movies);
            })
            .catch(err => {
                console.error("Failed to load movies:", err);
                grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--danger);"><i class="fa-solid fa-triangle-exclamation fa-2x"></i><p style="margin-top: 1rem;">Failed to fetch movies.</p></div>';
            });
    }

    function renderMovies(movies) {
        const grid = document.getElementById("movies-grid");
        grid.innerHTML = "";

        if (movies.length === 0) {
            grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 3rem;"><i class="fa-solid fa-circle-info fa-2x"></i><p style="margin-top: 1rem;">No watched movies found matching exclusion criteria.</p></div>';
            return;
        }

        movies.forEach(movie => {
            const card = document.createElement("div");
            card.className = "media-card";
            
            // Poster URL proxied through backend
            const posterUrl = `/api/posterproxy?server_type=${movie.server_type}&item_id=${movie.id}`;
            const sizeFormatted = formatBytes(movie.original_size);
            
            card.innerHTML = `
                <div class="media-badge ${movie.status}">${movie.status.toUpperCase()}</div>
                <div class="media-poster-container">
                    <img src="${posterUrl}" class="media-poster" alt="${movie.title}" onerror="this.src='https://placehold.co/400x600/101017/8a2be2?text=${encodeURIComponent(movie.title)}'">
                </div>
                <div class="media-info">
                    <div class="media-title" title="${movie.title}">${movie.title}</div>
                    <div class="media-meta">
                        <span>${movie.year || ""}</span>
                        <span>${sizeFormatted}</span>
                    </div>
                </div>
            `;
            
            // Click to trigger action
            card.addEventListener("click", () => {
                promptAction(
                    movie.status === "archived" ? "restore" : "spektor",
                    movie.server_type,
                    movie.id,
                    movie.title,
                    movie.file_path,
                    movie.original_size
                );
            });

            grid.appendChild(card);
        });
    }

    // Dynamic Filter Movies Input
    document.getElementById("movie-search").addEventListener("input", (e) => {
        const query = e.target.value.toLowerCase();
        const filtered = currentMovies.filter(m => m.title.toLowerCase().includes(query));
        renderMovies(filtered);
    });

    // -----------------------------------------------------------------------
    // Tab 3: TV Shows Logic
    // -----------------------------------------------------------------------
    
    function loadShows() {
        const grid = document.getElementById("shows-grid");
        grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted);"><i class="fa-solid fa-spinner fa-spin fa-2x"></i><p style="margin-top: 1rem;">Scanning TV shows library...</p></div>';

        fetch("/api/shows")
            .then(res => res.json())
            .then(shows => {
                currentShows = shows;
                renderShows(shows);
            })
            .catch(err => {
                console.error("Failed to load shows:", err);
                grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--danger);"><i class="fa-solid fa-triangle-exclamation fa-2x"></i><p style="margin-top: 1rem;">Failed to fetch TV shows.</p></div>';
            });
    }

    function renderShows(shows) {
        const grid = document.getElementById("shows-grid");
        grid.innerHTML = "";

        if (shows.length === 0) {
            grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 3rem;"><i class="fa-solid fa-circle-info fa-2x"></i><p style="margin-top: 1rem;">No TV shows found matching configuration.</p></div>';
            return;
        }

        shows.forEach(show => {
            const card = document.createElement("div");
            card.className = "media-card";
            
            const posterUrl = `/api/posterproxy?server_type=${show.server_type}&item_id=${show.id}`;
            
            card.innerHTML = `
                <div class="media-poster-container">
                    <img src="${posterUrl}" class="media-poster" alt="${show.title}" onerror="this.src='https://placehold.co/400x600/101017/8a2be2?text=${encodeURIComponent(show.title)}'">
                </div>
                <div class="media-info">
                    <div class="media-title" title="${show.title}">${show.title}</div>
                    <div class="media-meta">
                        <span>${show.year || ""}</span>
                        <span>${show.server_type.toUpperCase()}</span>
                    </div>
                </div>
            `;
            
            // Show click handler -> Open Seasons
            card.addEventListener("click", () => {
                openSeasonsModal(show.server_type, show.id, show.title);
            });

            grid.appendChild(card);
        });
    }

    // Dynamic Filter TV Shows Input
    document.getElementById("show-search").addEventListener("input", (e) => {
        const query = e.target.value.toLowerCase();
        const filtered = currentShows.filter(s => s.title.toLowerCase().includes(query));
        renderShows(filtered);
    });

    // TV Show Seasons Navigation
    function openSeasonsModal(serverType, showId, showTitle) {
        document.getElementById("seasons-title").textContent = `${showTitle} — Seasons`;
        const grid = document.getElementById("seasons-grid");
        grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted);"><i class="fa-solid fa-spinner fa-spin fa-2x"></i></div>';
        
        openModal(modalSeasons);

        fetch(`/api/shows/${serverType}/${showId}/seasons`)
            .then(res => res.json())
            .then(seasons => {
                grid.innerHTML = "";
                seasons.forEach(season => {
                    const card = document.createElement("div");
                    card.className = "season-card";
                    const posterUrl = `/api/posterproxy?server_type=${serverType}&item_id=${season.id}`;
                    
                    card.innerHTML = `
                        <div class="season-poster-container">
                            <img src="${posterUrl}" class="season-poster" alt="${season.title}" onerror="this.src='https://placehold.co/400x600/101017/8a2be2?text=${encodeURIComponent(season.title)}'">
                        </div>
                        <div class="season-info">
                            <div class="season-title">${season.title}</div>
                        </div>
                    `;
                    
                    // Click Season -> Load Episodes
                    card.addEventListener("click", () => {
                        closeModal(modalSeasons);
                        openEpisodesModal(serverType, showId, showTitle, season.id, season.title);
                    });
                    
                    grid.appendChild(card);
                });
            })
            .catch(err => {
                console.error("Failed to load seasons:", err);
                grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--danger);"><p>Failed to load seasons.</p></div>';
            });
    }

    // Season Episodes Table Navigation
    function openEpisodesModal(serverType, showId, showTitle, seasonId, seasonTitle) {
        document.getElementById("episodes-title").textContent = `${showTitle} — ${seasonTitle}`;
        const list = document.getElementById("episodes-list");
        list.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);"><i class="fa-solid fa-spinner fa-spin fa-2x"></i></td></tr>';
        
        openModal(modalEpisodes);

        fetch(`/api/shows/${serverType}/${showId}/seasons/${seasonId}/episodes`)
            .then(res => res.json())
            .then(episodes => {
                list.innerHTML = "";
                if (episodes.length === 0) {
                    list.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No watched episodes found in this season.</td></tr>';
                    return;
                }
                
                episodes.forEach(ep => {
                    const row = document.createElement("tr");
                    const sizeFormatted = formatBytes(ep.original_size);
                    
                    let actionButton = "";
                    if (ep.status === "archived") {
                        actionButton = `<button class="btn btn-success btn-xs" onclick="window.triggerEpisodeAction('restore', '${serverType}', '${ep.id}', '${showTitle} - S01E${ep.episode_number}', '${ep.file_path.replace(/\\/g, '\\\\')}', ${ep.original_size})"><i class="fa-solid fa-rotate-left"></i> Restore</button>`;
                    } else {
                        actionButton = `<button class="btn btn-primary btn-xs" onclick="window.triggerEpisodeAction('spektor', '${serverType}', '${ep.id}', '${showTitle} - S01E${ep.episode_number}', '${ep.file_path.replace(/\\/g, '\\\\')}', ${ep.original_size})"><i class="fa-solid fa-ghost"></i> Spektor</button>`;
                    }

                    row.innerHTML = `
                        <td>${ep.episode_number}</td>
                        <td>${ep.title}</td>
                        <td>${sizeFormatted}</td>
                        <td><span class="media-badge ${ep.status}" style="position: static; display: inline-block;">${ep.status.toUpperCase()}</span></td>
                        <td>${actionButton}</td>
                    `;
                    list.appendChild(row);
                });
            })
            .catch(err => {
                console.error("Failed to load episodes:", err);
                list.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--danger);">Failed to load episodes.</td></tr>';
            });
    }

    // Expose episode button callback to global scope
    window.triggerEpisodeAction = (action, serverType, itemId, title, filePath, size) => {
        closeModal(modalEpisodes);
        promptAction(action, serverType, itemId, title, filePath, size);
    };

    // -----------------------------------------------------------------------
    // Action Confirmation & Execute
    // -----------------------------------------------------------------------
    
    function promptAction(action, serverType, itemId, title, filePath, size) {
        pendingAction = { type: action, serverType, itemId, title, filePath, size };
        
        const isRestore = action === "restore";
        document.getElementById("confirm-msg").innerHTML = `Are you sure you want to <strong>${isRestore ? 'RESTORE' : 'SPEKTOR'}</strong> item:<br><strong>${title}</strong>?`;
        document.getElementById("confirm-file-path").textContent = filePath || "N/A";
        document.getElementById("confirm-saved-size").textContent = formatBytes(size);
        
        const execBtn = document.getElementById("btn-confirm-execute");
        if (isRestore) {
            execBtn.className = "btn btn-success";
            execBtn.textContent = "Restore";
        } else {
            execBtn.className = "btn btn-danger";
            execBtn.textContent = "Confirm Spektor";
        }
        
        openModal(modalConfirm);
    }

    // Cancel Confirm Modal
    document.getElementById("btn-confirm-cancel").addEventListener("click", () => {
        closeModal(modalConfirm);
    });

    // Execute Confirmed Action
    document.getElementById("btn-confirm-execute").addEventListener("click", () => {
        closeModal(modalConfirm);
        
        const { type, serverType, itemId, title } = pendingAction;
        const endpoint = type === "restore" ? "/api/restore" : "/api/spektor";
        
        showToast(`${type === 'restore' ? 'Queued restoration' : 'Queued archival'} for '${title}'...`, "warning");

        fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ server_type: serverType, item_id: itemId })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                showToast(`Action started successfully. Check dashboard log.`, "success");
                
                // Reload active grid after a short delay
                setTimeout(() => {
                    const activeTab = document.querySelector(".nav-item.active").getAttribute("data-tab");
                    handleTabActivation(activeTab);
                }, 2000);
            } else {
                showToast(data.message || "Failed to start background task.", "error");
            }
        })
        .catch(err => {
            console.error("Action fetch failed:", err);
        });
    });

    // -----------------------------------------------------------------------
    // Tab 4: Settings Logic
    // -----------------------------------------------------------------------
    
    function loadSettings() {
        fetch("/api/config")
            .then(res => res.json())
            .then(config => {
                // Populate Servers
                const plex = (config.servers || []).find(s => s.type === "plex") || {};
                const jellyfin = (config.servers || []).find(s => s.type === "jellyfin") || {};
                const emby = (config.servers || []).find(s => s.type === "emby") || {};

                document.getElementById("srv-plex-enabled").checked = !!plex.enabled;
                document.getElementById("srv-plex-url").value = plex.url || "";
                document.getElementById("srv-plex-token").value = plex.token || "";
                document.getElementById("srv-plex-libs").value = (plex.libraries || []).join(", ");

                document.getElementById("srv-jellyfin-enabled").checked = !!jellyfin.enabled;
                document.getElementById("srv-jellyfin-url").value = jellyfin.url || "";
                document.getElementById("srv-jellyfin-key").value = jellyfin.api_key || "";
                document.getElementById("srv-jellyfin-user").value = jellyfin.user_id || "";
                document.getElementById("srv-jellyfin-libs").value = (jellyfin.libraries || []).join(", ");

                document.getElementById("srv-emby-enabled").checked = !!emby.enabled;
                document.getElementById("srv-emby-url").value = emby.url || "";
                document.getElementById("srv-emby-key").value = emby.api_key || "";
                document.getElementById("srv-emby-user").value = emby.user_id || "";
                document.getElementById("srv-emby-libs").value = (emby.libraries || []).join(", ");

                // Populate Rules
                const rules = config.rules || {};
                document.getElementById("rule-min-age").value = rules.min_age_days !== undefined ? rules.min_age_days : 7;
                document.getElementById("rule-threshold").value = rules.dummy_threshold_mb !== undefined ? rules.dummy_threshold_mb : 15;
                document.getElementById("rule-exclude-labels").value = (rules.exclude_labels || []).join(", ");
                document.getElementById("rule-exclude-genres").value = (rules.exclude_genres || []).join(", ");

                // Populate Safety
                const safety = config.safety || {};
                document.getElementById("safety-dry-run").checked = safety.dry_run !== undefined ? !!safety.dry_run : true;
                document.getElementById("safety-backup").checked = !!safety.backup_original_media;
                document.getElementById("safety-backup-dir").value = safety.backup_directory || "";

                // Populate Integrations
                const integrations = config.integrations || {};
                const radarr = integrations.radarr || {};
                const sonarr = integrations.sonarr || {};

                document.getElementById("int-radarr-enabled").checked = !!radarr.enabled;
                document.getElementById("int-radarr-url").value = radarr.url || "";
                document.getElementById("int-radarr-key").value = radarr.api_key || "";

                document.getElementById("int-sonarr-enabled").checked = !!sonarr.enabled;
                document.getElementById("int-sonarr-url").value = sonarr.url || "";
                document.getElementById("int-sonarr-key").value = sonarr.api_key || "";

                // Populate Aesthetics
                const aesthetics = config.aesthetics || {};
                document.getElementById("aes-overlay-enabled").checked = aesthetics.enable_poster_overlay !== undefined ? !!aesthetics.enable_poster_overlay : true;
                document.getElementById("aes-banner-color").value = (aesthetics.banner_color || [20, 20, 20, 204]).join(", ");
                document.getElementById("aes-border-color").value = (aesthetics.border_color || [212, 175, 55, 255]).join(", ");
                document.getElementById("aes-font-name").value = aesthetics.font_name || "Arial";
                document.getElementById("aes-font-size").value = aesthetics.font_size_ratio !== undefined ? aesthetics.font_size_ratio : 0.045;
            })
            .catch(err => {
                console.error("Failed to load settings:", err);
                showToast("Failed to fetch settings from server.", "error");
            });
    }

    document.getElementById("btn-save-settings").addEventListener("click", () => {
        // Build the configuration structure back
        const config = {
            servers: [
                {
                    type: "plex",
                    enabled: document.getElementById("srv-plex-enabled").checked,
                    url: document.getElementById("srv-plex-url").value.trim(),
                    token: document.getElementById("srv-plex-token").value.trim(),
                    libraries: document.getElementById("srv-plex-libs").value.split(",").map(s => s.trim()).filter(Boolean)
                },
                {
                    type: "jellyfin",
                    enabled: document.getElementById("srv-jellyfin-enabled").checked,
                    url: document.getElementById("srv-jellyfin-url").value.trim(),
                    api_key: document.getElementById("srv-jellyfin-key").value.trim(),
                    user_id: document.getElementById("srv-jellyfin-user").value.trim(),
                    libraries: document.getElementById("srv-jellyfin-libs").value.split(",").map(s => s.trim()).filter(Boolean)
                },
                {
                    type: "emby",
                    enabled: document.getElementById("srv-emby-enabled").checked,
                    url: document.getElementById("srv-emby-url").value.trim(),
                    api_key: document.getElementById("srv-emby-key").value.trim(),
                    user_id: document.getElementById("srv-emby-user").value.trim(),
                    libraries: document.getElementById("srv-emby-libs").value.split(",").map(s => s.trim()).filter(Boolean)
                }
            ],
            rules: {
                min_age_days: parseInt(document.getElementById("rule-min-age").value) || 0,
                exclude_labels: document.getElementById("rule-exclude-labels").value.split(",").map(s => s.trim()).filter(Boolean),
                exclude_genres: document.getElementById("rule-exclude-genres").value.split(",").map(s => s.trim()).filter(Boolean),
                dummy_threshold_mb: parseInt(document.getElementById("rule-threshold").value) || 0
            },
            aesthetics: {
                enable_poster_overlay: document.getElementById("aes-overlay-enabled").checked,
                banner_color: document.getElementById("aes-banner-color").value.split(",").map(s => parseInt(s.trim())).filter(n => !isNaN(n)),
                border_color: document.getElementById("aes-border-color").value.split(",").map(s => parseInt(s.trim())).filter(n => !isNaN(n)),
                font_name: document.getElementById("aes-font-name").value.trim(),
                font_size_ratio: parseFloat(document.getElementById("aes-font-size").value) || 0.045
            },
            integrations: {
                radarr: {
                    enabled: document.getElementById("int-radarr-enabled").checked,
                    url: document.getElementById("int-radarr-url").value.trim(),
                    api_key: document.getElementById("int-radarr-key").value.trim()
                },
                sonarr: {
                    enabled: document.getElementById("int-sonarr-enabled").checked,
                    url: document.getElementById("int-sonarr-url").value.trim(),
                    api_key: document.getElementById("int-sonarr-key").value.trim()
                }
            },
            safety: {
                dry_run: document.getElementById("safety-dry-run").checked,
                backup_original_media: document.getElementById("safety-backup").checked,
                backup_directory: document.getElementById("safety-backup-dir").value.trim()
            }
        };

        // Basic verification
        if (config.aesthetics.banner_color.length !== 4 || config.aesthetics.border_color.length !== 4) {
            showToast("Banner and border colors must be 4 numbers (RGBA, e.g. 255, 255, 255, 255).", "warning");
            return;
        }

        fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                showToast("Configuration saved and reloaded successfully!", "success");
            } else {
                showToast(data.detail || "Failed to save configuration.", "error");
            }
        })
        .catch(err => {
            console.error("Failed to save configuration:", err);
            showToast("Network error saving configuration.", "error");
        });
    });

    // -----------------------------------------------------------------------
    // Initial Load
    // -----------------------------------------------------------------------
    loadDashboardData();
    startLogPolling();
});
