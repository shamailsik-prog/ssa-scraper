async function seekArchivedGridAbsolute(table, startRow, maxRows) {
    const toInt = (value, fallback = 0) => {
        const n = Number(value);
        if (!Number.isFinite(n)) return fallback;
        return Math.max(0, Math.floor(n));
    };
    const requestedStartRow = toInt(startRow, 0);
    const result = {
        requested_start_row: requestedStartRow,
        start_row: 0,
        total_rows: null,
        page_length: null,
        seek_mode: "none",
    };
    const root = typeof globalThis !== "undefined" ? globalThis : {};
    const jq = (root.jQuery || root.$ || (root.window && (root.window.jQuery || root.window.$))) || null;

    const readDomRows = () => {
        if (!table) return [];
        try {
            const tbody = (table.tBodies && table.tBodies[0])
                || (typeof table.querySelector === "function" ? table.querySelector("tbody") : null);
            if (!tbody) return [];
            if (tbody.rows) return tbody.rows;
            if (typeof tbody.length === "number") return tbody;
        } catch (_domError) {
            return [];
        }
        return [];
    };

    // Primary live path (New Bot / droplet prove): no jQuery / no DataTables; the
    // full ~20k <tr> list is already in the DOM. Slice at row_offset and expose
    // seek_mode=dom_absolute ONLY when the offset-th row is confirmed present.
    // DataTables is an optional fallback when that row is not in the DOM.
    const applyDomAbsolute = () => {
        const rows = readDomRows();
        const length = rows && typeof rows.length === "number" ? rows.length : 0;
        if (!(requestedStartRow < length && rows[requestedStartRow])) {
            return false;
        }
        const windowLen = Math.max(1, toInt(maxRows, 1) || 1);
        result.total_rows = length;
        result.page_length = windowLen;
        const target = rows[requestedStartRow];
        if (target && typeof target.scrollIntoView === "function") {
            try {
                target.scrollIntoView({ block: "nearest" });
            } catch (_scrollError) {
                // Slice/index is the source of truth; scroll is best-effort.
            }
        }
        result.start_row = requestedStartRow;
        result.seek_mode = "dom_absolute";
        return true;
    };

    const resolveApi = () => {
        if (!jq || !jq.fn || !jq.fn.dataTable) return null;
        const attempts = [
            () => jq(table).DataTable(),
            () => {
                const legacy = jq(table).dataTable();
                return legacy && typeof legacy.api === "function" ? legacy.api() : null;
            },
            () => (jq.fn.dataTable.Api ? new jq.fn.dataTable.Api(table) : null),
        ];
        for (let i = 0; i < attempts.length; i += 1) {
            try {
                const api = attempts[i]();
                if (api && typeof api.page === "function") return api;
            } catch (_apiError) {
                // Try the next DataTables retrieval path.
            }
        }
        return null;
    };

    const isDataTableControlling = () => {
        try {
            return Boolean(
                jq
                && jq.fn
                && jq.fn.dataTable
                && typeof jq.fn.dataTable.isDataTable === "function"
                && jq.fn.dataTable.isDataTable(table)
            );
        } catch (_dtCheckError) {
            return false;
        }
    };

    if (applyDomAbsolute()) {
        return result;
    }

    const api = resolveApi();
    const dtControlling = Boolean(api) || isDataTableControlling();
    if (!dtControlling) {
        result.start_row = 0;
        result.seek_mode = "unavailable";
        return result;
    }
    if (!api) {
        result.seek_mode = "unavailable";
        return result;
    }

    const readInfo = () => {
        try {
            return (typeof api.page.info === "function" ? api.page.info() : {}) || {};
        } catch (_infoError) {
            return {};
        }
    };

    const infoBefore = readInfo();
    const recordsBefore = Number(infoBefore.recordsDisplay ?? infoBefore.recordsTotal);
    if (Number.isFinite(recordsBefore) && recordsBefore >= 0) {
        result.total_rows = Math.floor(recordsBefore);
    }
    let pageLength = Number(
        infoBefore.length ?? (typeof api.page.len === "function" ? api.page.len() : Number.NaN)
    );
    if (!Number.isFinite(pageLength) || pageLength <= 0) {
        pageLength = Math.max(1, toInt(maxRows, 1) || 1);
    }
    result.page_length = Math.floor(pageLength);

    const boundedStart = result.total_rows && result.total_rows > 0
        ? Math.min(requestedStartRow, Math.max(result.total_rows - 1, 0))
        : requestedStartRow;
    const targetPage = Math.floor(boundedStart / pageLength);
    const targetStart = targetPage * pageLength;

    const applyAjaxStart = (start) => {
        try {
            const settings0 = api.settings && typeof api.settings === "function" ? api.settings()[0] : null;
            if (!settings0) return false;
            settings0._iDisplayStart = start;
            if (settings0.oAjaxData && typeof settings0.oAjaxData === "object") {
                settings0.oAjaxData.start = start;
                settings0.oAjaxData.length = pageLength;
            }
            if (settings0.ajax && typeof settings0.ajax === "object" && settings0.ajax.data && typeof settings0.ajax.data === "object") {
                settings0.ajax.data.start = start;
            }
            return true;
        } catch (_ajaxError) {
            return false;
        }
    };

    applyAjaxStart(targetStart);

    const waitMs = toInt(root.ARCHIVED_GRID_SEEK_TIMEOUT_MS, 8000) || 8000;
    await new Promise((resolve) => {
        let done = false;
        const finish = (mode) => {
            if (!done) {
                done = true;
                resolve(mode);
            }
        };
        try {
            if (jq && typeof jq === "function") {
                jq(table).one("draw.dt", () => finish("draw"));
            }
            if (typeof api.one === "function") {
                api.one("draw", () => finish("draw"));
            }
        } catch (_bindError) {
            // Continue; landing is confirmed from page.info().start, not the event.
        }
        try {
            // Prefer DataTables absolute seek: api().page(Math.floor(start/pageLength)).draw(false)
            api.page(Math.floor(boundedStart / pageLength)).draw(false);
        } catch (_pageError) {
            try {
                api.draw(false);
            } catch (_drawError) {
                finish("error");
                return;
            }
        }
        setTimeout(() => finish("timeout"), waitMs);
    });

    const infoAfter = readInfo();
    const recordsAfter = Number(infoAfter.recordsDisplay ?? infoAfter.recordsTotal);
    if (Number.isFinite(recordsAfter) && recordsAfter >= 0) {
        result.total_rows = Math.floor(recordsAfter);
    }
    const afterLen = Number(infoAfter.length ?? pageLength);
    if (Number.isFinite(afterLen) && afterLen > 0) {
        result.page_length = Math.floor(afterLen);
        pageLength = result.page_length;
    }
    const afterStart = Number(infoAfter.start);
    if (Number.isFinite(afterStart) && afterStart >= 0) {
        const windowLen = result.page_length || pageLength;
        const inWindow = afterStart <= requestedStartRow && requestedStartRow < (afterStart + windowLen);
        const landed = afterStart === targetStart || afterStart === boundedStart || inWindow;
        if (landed) {
            result.start_row = Math.floor(afterStart);
            result.seek_mode = "datatable";
            return result;
        }
    }

    // Failed to confirm an absolute window. Never invent start_row from a DOM index.
    result.start_row = 0;
    result.seek_mode = "none";
    return result;
}
