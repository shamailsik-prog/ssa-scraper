"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const seekPath = path.resolve(__dirname, "../../scraper/auth/archived_grid_seek.js");
const seekSource = fs.readFileSync(seekPath, "utf8");

function loadSeek(sandboxExtras) {
    const sandbox = {
        console,
        setTimeout,
        clearTimeout,
        ...sandboxExtras,
    };
    sandbox.globalThis = sandbox;
    sandbox.window = sandbox.window || sandbox;
    vm.createContext(sandbox);
    vm.runInContext(`${seekSource}\nthis.seekArchivedGridAbsolute = seekArchivedGridAbsolute;`, sandbox);
    return sandbox.seekArchivedGridAbsolute;
}

function createDataTableEnv({ start = 0, pageLength = 10, total = 20567, fireDraw = true } = {}) {
    const state = {
        start,
        pageLength,
        total,
        oAjaxData: { start, length: pageLength },
    };
    const apiListeners = [];
    const jqListeners = {};

    function pageFn(n) {
        if (typeof n === "undefined") {
            return Math.floor(state.start / state.pageLength);
        }
        state.start = n * state.pageLength;
        state.oAjaxData.start = state.start;
        state.oAjaxData.length = state.pageLength;
        return api;
    }
    pageFn.info = () => ({
        start: state.start,
        length: state.pageLength,
        recordsTotal: state.total,
        recordsDisplay: state.total,
        page: Math.floor(state.start / state.pageLength),
    });
    pageFn.len = () => state.pageLength;

    const api = {
        page: pageFn,
        draw() {
            if (fireDraw) {
                apiListeners.splice(0).forEach((fn) => fn());
                (jqListeners["draw.dt"] || []).splice(0).forEach((fn) => fn());
            }
            return api;
        },
        one(event, fn) {
            apiListeners.push(fn);
            return api;
        },
        settings() {
            return [
                {
                    oFeatures: { bServerSide: true },
                    _iDisplayStart: state.start,
                    oAjaxData: state.oAjaxData,
                    ajax: { url: "/Login/GetArchivedPatientGrid", data: state.oAjaxData },
                },
            ];
        },
    };

    function jq() {
        return {
            DataTable: () => api,
            dataTable: () => ({ api: () => api }),
            one(event, fn) {
                jqListeners[event] = jqListeners[event] || [];
                jqListeners[event].push(fn);
                return this;
            },
        };
    }
    jq.fn = {
        dataTable: {
            isDataTable: () => true,
            Api: function Api() {
                return api;
            },
        },
    };

    return { api, state, jq };
}

function createDomTable(rowCount) {
    const rows = [];
    for (let i = 0; i < rowCount; i += 1) {
        rows.push({
            index: i,
            scrolled: false,
            scrollIntoView() {
                this.scrolled = true;
            },
        });
    }
    return {
        id: "archivedpatientGrid",
        tBodies: [{ rows }],
        querySelector(sel) {
            return sel === "tbody" ? this.tBodies[0] : null;
        },
    };
}

async function run() {
    const table = { id: "archivedpatientGrid" };

    // Primary live path: no jQuery / no DataTables; full ~20k <tr> list in DOM.
    {
        const liveTable = createDomTable(20567);
        const seek = loadSeek({ ARCHIVED_GRID_SEEK_TIMEOUT_MS: 20 });
        const result = await seek(liveTable, 141, 200);
        assert.strictEqual(result.seek_mode, "dom_absolute", `expected live DOM-slice seek, got ${JSON.stringify(result)}`);
        assert.strictEqual(result.start_row, 141, "droplet-proved offset must land as start_row");
        assert.strictEqual(result.requested_start_row, 141);
        assert.strictEqual(result.total_rows, 20567);
        assert.strictEqual(result.page_length, 200);
        assert.strictEqual(liveTable.tBodies[0].rows[141].scrolled, true);
    }

    {
        const liveTable = createDomTable(20567);
        const seek = loadSeek({ ARCHIVED_GRID_SEEK_TIMEOUT_MS: 20 });
        const result = await seek(liveTable, 200, 200);
        assert.strictEqual(result.seek_mode, "dom_absolute", `expected dom_absolute on live non-DT grid, got ${JSON.stringify(result)}`);
        assert.strictEqual(result.start_row, 200);
        assert.strictEqual(result.requested_start_row, 200);
        assert.strictEqual(result.total_rows, 20567);
        assert.strictEqual(result.page_length, 200);
        assert.strictEqual(liveTable.tBodies[0].rows[200].scrolled, true);
    }

    {
        const shortTable = createDomTable(10);
        const seek = loadSeek({ ARCHIVED_GRID_SEEK_TIMEOUT_MS: 20 });
        const result = await seek(shortTable, 200, 200);
        assert.notStrictEqual(result.seek_mode, "dom_absolute");
        assert.notStrictEqual(result.seek_mode, "datatable");
        assert.strictEqual(result.start_row, 0, "must not invent start_row from first-page DOM when the offset-th row is missing");
        assert.strictEqual(result.requested_start_row, 200);
    }

    // Optional DataTables fallback: only when the offset-th <tr> is not already in the DOM.
    {
        const env = createDataTableEnv({ start: 0, pageLength: 10, total: 20567 });
        const seek = loadSeek({ jQuery: env.jq, $: env.jq, ARCHIVED_GRID_SEEK_TIMEOUT_MS: 50 });
        const result = await seek(table, 200, 200);
        assert.strictEqual(result.seek_mode, "datatable", `expected datatable fallback, got ${JSON.stringify(result)}`);
        assert.strictEqual(result.start_row, 200);
        assert.strictEqual(result.requested_start_row, 200);
        assert.strictEqual(result.page_length, 10);
        assert.strictEqual(result.total_rows, 20567);
        assert.strictEqual(env.state.oAjaxData.start, 200);
        assert.strictEqual(env.api.settings()[0]._iDisplayStart, 200);
    }

    {
        const env = createDataTableEnv({ start: 0, pageLength: 10, total: 20567 });
        const seek = loadSeek({ jQuery: env.jq, $: env.jq, ARCHIVED_GRID_SEEK_TIMEOUT_MS: 50 });
        const result = await seek(table, 205, 200);
        assert.strictEqual(result.seek_mode, "datatable");
        assert.strictEqual(result.start_row, 200, "start_row must be the page window start, not the DOM index");
        assert.strictEqual(env.state.oAjaxData.start, 200);
    }

    {
        const env = createDataTableEnv({ start: 0, pageLength: 10, total: 20567, fireDraw: false });
        const seek = loadSeek({ jQuery: env.jq, $: env.jq, ARCHIVED_GRID_SEEK_TIMEOUT_MS: 20 });
        const result = await seek(table, 200, 200);
        assert.strictEqual(result.seek_mode, "datatable", "landing is confirmed from page.info().start even without draw.dt");
        assert.strictEqual(result.start_row, 200);
    }

    {
        const seek = loadSeek({ ARCHIVED_GRID_SEEK_TIMEOUT_MS: 20 });
        const result = await seek(table, 200, 200);
        assert.strictEqual(result.seek_mode, "unavailable");
        assert.strictEqual(result.start_row, 0, "must not invent start_row from a DOM index when DataTables is missing and the offset-th row is absent");
        assert.strictEqual(result.requested_start_row, 200);
    }

    {
        const env = createDataTableEnv({ start: 0, pageLength: 10, total: 20567 });
        env.api.page = function brokenPage() {
            throw new Error("page() unavailable");
        };
        env.api.page.info = () => ({
            start: 0,
            length: 10,
            recordsTotal: 20567,
            recordsDisplay: 20567,
        });
        env.api.draw = () => {
            throw new Error("draw() unavailable");
        };
        const seek = loadSeek({ jQuery: env.jq, $: env.jq, ARCHIVED_GRID_SEEK_TIMEOUT_MS: 20 });
        const result = await seek(table, 200, 200);
        assert.strictEqual(result.start_row, 0, "failed API seek must keep start_row at 0 when the offset-th row is not in the DOM");
        assert.notStrictEqual(result.seek_mode, "datatable");
        assert.notStrictEqual(result.seek_mode, "dom_absolute");
    }

    {
        const env = createDataTableEnv({ start: 0, pageLength: 10, total: 20567 });
        const livePlusDt = createDomTable(20567);
        const seek = loadSeek({ jQuery: env.jq, $: env.jq, ARCHIVED_GRID_SEEK_TIMEOUT_MS: 20 });
        const result = await seek(livePlusDt, 141, 200);
        assert.strictEqual(result.seek_mode, "dom_absolute", "live DOM slice is primary even if DataTables is also present");
        assert.strictEqual(result.start_row, 141);
        assert.strictEqual(env.state.oAjaxData.start, 0, "must not touch DataTables ajax start= when the offset-th <tr> is already in the DOM");
    }

    console.log("archived_grid_seek.js: all assertions passed");
}

run().catch((err) => {
    console.error(err);
    process.exit(1);
});
