// tools/native-memory-export/inventory-export.h
//
// Read-only account card inventory sidecar for the IL2CPP memory exporter.
//
// This header is included from exporter.cpp *inside* its anonymous namespace,
// immediately after the shared reflection/serialization/publication helpers and
// before ExportWorker, so the existing primitives (Invoke, ReadStringField,
// ReadIntField, GetField, FindMethodInHierarchy, JsonEscape, SnapshotHash,
// WriteFileBytes, OutputDirectory, g_publishMutex, ElapsedSince and the polling
// constants) are available with no extra build input. Every new function and
// every piece of new state lives in that same anonymous namespace.
//
// Scope of this sidecar (deliberately narrow):
//   * readonly account card inventory only: UserDataManagerBase
//     get_UserIdolCardList (fallback get__userIdolCardList) and
//     get_UserSupportCardList (fallback get__userSupportCardList), each read as
//     the inherited GetAllList(null) list and then list Count/Item, exactly the
//     shape the existing memory export already uses.
//   * native PascalCase row field names and raw string ids are preserved; there
//     is no local-id mapping, no balance/auth/profile/player-id read and no
//     other collection is touched.
//
// Data readiness model (non-blocking):
//   * ProbeCardInventory() performs AT MOST ONE pair read (no Sleep, no loop) per
//     call and reports pending until two consecutive complete serialized pair
//     snapshots, separated by >= 500 ms, are identical. Only the previous fully
//     serialized pair and its tick are retained (no managed objects).
//   * An account is data-ready only when the role (idol card) list has at least
//     one row. Both lists being empty during login is NOT a valid empty account,
//     so that state keeps waiting.
//   * A support list of zero rows is valid once the role list is non-empty.
//   * A null collection/list/count just waits; a missing getter/accessor, an
//     Invoke exception or an invalid/missing required row field is a contained
//     terminal failure. Every collection row is retained: a zero stock/rank
//     value never means "unowned".
//
// Publication model:
//   * Success writes an immutable snapshot directory
//     <output>/card-inventory/snapshots/<pid>-<workerStartMs>/ containing
//     idol-cards.json, support-cards.json and manifest.json (written last), then
//     renames the sibling temp directory to the final name and only then
//     rewrites card-inventory/status.json. Snapshot files are validated by
//     sha256 and the status file commits the same relative directory, counts and
//     hashes.
//   * Pre-existing snapshots are never deleted or modified; there is no
//     recursive delete anywhere in this header, and a failed run publishes an
//     incomplete status (counts null) without touching old data.
//   * Arrays are never written into the main memories.json protocol.
//   * g_inventoryTerminalPublished makes the first terminal inventory result
//     win, independently of the memory export's own g_terminalPublished.
//     Publication serializes on the existing g_publishMutex but never calls
//     WriteResults while that mutex is held.

#pragma once

constexpr DWORD kInventoryStableGapMs         = 500;
constexpr int   kInventoryMaxRows             = kMaxRecords;
const char* const kInventoryPluginVersion     = "1.0.8-inventory";
const char* const kInventoryPendingMessage    = "waiting for game account card data (maximum 180 seconds)";
const char* const kInventoryUnfinishedMessage = "export not reached or unfinished";
const char* const kInventoryTimeoutMessage    = "inventory watchdog deadline reached";
const char* const kInventoryLateMessage       = "inventory completed after diagnostic deadline; snapshot not published";
const char* const kInventoryNotStartedMessage = "worker thread not started";
const char* const kInventoryClosedMessage     = "inventory terminal result already published";

enum class InventoryReadState { Ready, NotReady, Failed };

struct InventoryExportResult {
    std::string status;
    std::string message;
    int         idolCards    = 0;
    int         supportCards = 0;
    bool        completed    = false;
    bool        terminal     = false;
};

// Fixed-label helper: only process-lifetime literals reach the diagnostic log.
const char* InventoryPhaseLabel(const std::string& status) {
    if (status == "running")    return "running";
    if (status == "completed")  return "completed";
    if (status == "error")      return "error";
    if (status == "timeout")    return "timeout";
    if (status == "not_loaded") return "not_loaded";
    if (status == "io_error")   return "io_error";
    return "closed";
}

// Independent of g_terminalPublished: memory completion does not define
// inventory completion and vice versa.
std::atomic<bool> g_inventoryTerminalPublished{false};

// Worker-thread-only stable-readiness candidate: just the previous serialized
// pair and the tick at which it was observed.
struct InventoryPairSnapshot {
    bool        valid = false;
    std::string idolJson;
    std::string supportJson;
    DWORD       tick  = 0;
};
InventoryPairSnapshot g_inventoryCandidate;

// ---- per-row serialization --------------------------------------------------
//
// Required fields: a missing/null/invalid/duplicate id or a failed required int
// aborts the inventory export with a fixed generic label (never the raw getter
// error, which may contain account data). Optional fields that are missing or
// fail to read export JSON null and are reported in field_issues with a fixed
// issue text; they never invalidate the row.

bool SerializeIdolCardRow(void* row, std::unordered_set<std::string>& seenIds,
                          std::ostringstream& out, std::string& error) {
    void* cls = g_api.object_get_class(row);
    if (!cls) { error = "idol-cards row class missing"; return false; }
    const Field fId     = GetField(cls, "get_IdolCardId");
    const Field fRank   = GetField(cls, "get_LevelLimitRank");
    const Field fPot    = GetField(cls, "get_PotentialRank");
    const Field fSkin   = GetField(cls, "get_IdolCardSkinId");   // optional string
    const Field fLevel  = GetField(cls, "get_Level");            // optional int
    const Field fLimit  = GetField(cls, "get_LevelLimit");       // optional int
    const Field fPlan   = GetField(cls, "get_PlanType");         // optional int/enum
    const Field fRarity = GetField(cls, "get_RarityInt");        // optional int

    std::string id, ignored;
    if (!ReadStringField(row, fId, id, ignored)) { error = "idol-cards IdolCardId read failed"; return false; }
    if (id.empty()) { error = "idol-cards IdolCardId empty"; return false; }
    if (!seenIds.insert(id).second) { error = "idol-cards duplicate IdolCardId"; return false; }
    int64_t rank = 0, potentialRank = 0;
    if (!ReadIntField(row, fRank, rank, ignored))         { error = "idol-cards LevelLimitRank read failed"; return false; }
    if (!ReadIntField(row, fPot, potentialRank, ignored)) { error = "idol-cards PotentialRank read failed"; return false; }

    std::ostringstream optional, issues;
    bool issued = false;
    auto noteIssue = [&issues, &issued](const char* name, const char* text) {
        issues << (issued ? "," : "") << "\"" << name << "\":\"" << text << "\"";
        issued = true;
    };
    auto optionalString = [&](const Field& field, const char* name) {
        std::string value, err;
        if (field.method && ReadStringField(row, field, value, err)) {
            optional << "\"" << name << "\":\"" << JsonEscape(value) << "\",";
        } else {
            optional << "\"" << name << "\":null,";
            noteIssue(name, field.method ? "read_failed" : "missing");
        }
    };
    auto optionalInt = [&](const Field& field, const char* name) {
        int64_t value = 0;
        std::string err;
        if (field.method && ReadIntField(row, field, value, err)) {
            optional << "\"" << name << "\":" << value << ",";
        } else {
            optional << "\"" << name << "\":null,";
            noteIssue(name, field.method ? "read_failed" : "missing");
        }
    };
    optionalString(fSkin, "IdolCardSkinId");
    optionalInt(fLevel,   "Level");
    optionalInt(fLimit,   "LevelLimit");
    optionalInt(fPlan,    "PlanType");
    optionalInt(fRarity,  "RarityInt");

    out << "{\"IdolCardId\":\"" << JsonEscape(id) << "\",";
    out << "\"LevelLimitRank\":" << rank << ",\"PotentialRank\":" << potentialRank << ",";
    out << optional.str();
    out << "\"field_issues\":{" << issues.str() << "}}";
    return true;
}

bool SerializeSupportCardRow(void* row, std::unordered_set<std::string>& seenIds,
                             std::ostringstream& out, std::string& error) {
    void* cls = g_api.object_get_class(row);
    if (!cls) { error = "support-cards row class missing"; return false; }
    const Field fId     = GetField(cls, "get_SupportCardId");
    const Field fLevel  = GetField(cls, "get_Level");
    const Field fRank   = GetField(cls, "get_LevelLimitRank");
    const Field fStock  = GetField(cls, "get_StockQuantity");
    const Field fLimit  = GetField(cls, "get_LevelLimit");  // optional int
    const Field fPlan   = GetField(cls, "get_PlanType");    // optional int/enum
    const Field fRarity = GetField(cls, "get_RarityInt");   // optional int

    std::string id, ignored;
    if (!ReadStringField(row, fId, id, ignored)) { error = "support-cards SupportCardId read failed"; return false; }
    if (id.empty()) { error = "support-cards SupportCardId empty"; return false; }
    if (!seenIds.insert(id).second) { error = "support-cards duplicate SupportCardId"; return false; }
    int64_t level = 0, rank = 0, stock = 0;
    if (!ReadIntField(row, fLevel, level, ignored)) { error = "support-cards Level read failed"; return false; }
    if (!ReadIntField(row, fRank, rank, ignored))   { error = "support-cards LevelLimitRank read failed"; return false; }
    if (!ReadIntField(row, fStock, stock, ignored)) { error = "support-cards StockQuantity read failed"; return false; }

    std::ostringstream optional, issues;
    bool issued = false;
    auto noteIssue = [&issues, &issued](const char* name, const char* text) {
        issues << (issued ? "," : "") << "\"" << name << "\":\"" << text << "\"";
        issued = true;
    };
    auto optionalInt = [&](const Field& field, const char* name) {
        int64_t value = 0;
        std::string err;
        if (field.method && ReadIntField(row, field, value, err)) {
            optional << "\"" << name << "\":" << value << ",";
        } else {
            optional << "\"" << name << "\":null,";
            noteIssue(name, field.method ? "read_failed" : "missing");
        }
    };
    optionalInt(fLimit,  "LevelLimit");
    optionalInt(fPlan,   "PlanType");
    optionalInt(fRarity, "RarityInt");

    out << "{\"SupportCardId\":\"" << JsonEscape(id) << "\",";
    out << "\"Level\":" << level << ",\"LevelLimitRank\":" << rank << ",\"StockQuantity\":" << stock << ",";
    out << optional.str();
    out << "\"field_issues\":{" << issues.str() << "}}";
    return true;
}

// ---- list read --------------------------------------------------------------
//
// Reads one complete list (every row) into a serialized JSON array. Invoke
// exceptions are a terminal failure (never an endless "not ready" retry); a null
// collection/list/count waits; a missing accessor or a missing row object for an
// advertised count is a terminal failure instead of a silent empty list.
InventoryReadState InventoryReadList(void* manager, void* managerClass,
                                     const char* getterName, const char* fallbackName,
                                     const char* label, bool idolCards,
                                     std::string& json, int& count, std::string& error) {
    count = 0;
    json.clear();
    if (!manager || !managerClass) return InventoryReadState::NotReady;

    void* getter = FindMethodInHierarchy(managerClass, getterName, 0);
    if (!getter && fallbackName) getter = FindMethodInHierarchy(managerClass, fallbackName, 0);
    if (!getter) { error = std::string(label) + " getter missing"; return InventoryReadState::Failed; }

    const auto rColl = Invoke(getter, manager, nullptr);
    if (!rColl.exc.empty()) { error = std::string(label) + " invocation failed"; return InventoryReadState::Failed; }
    if (!rColl.value) return InventoryReadState::NotReady;

    void* collClass = g_api.object_get_class(rColl.value);
    if (!collClass) return InventoryReadState::NotReady;
    void* getAllList = FindMethodInHierarchy(collClass, "GetAllList", 1);
    if (!getAllList) { error = std::string(label) + " GetAllList missing"; return InventoryReadState::Failed; }

    void* argsAll[1] = { nullptr };
    const auto rList = Invoke(getAllList, rColl.value, argsAll);
    if (!rList.exc.empty()) { error = std::string(label) + " GetAllList invocation failed"; return InventoryReadState::Failed; }
    if (!rList.value) return InventoryReadState::NotReady;

    void* listClass = g_api.object_get_class(rList.value);
    if (!listClass) return InventoryReadState::NotReady;
    void* getCount = FindMethodInHierarchy(listClass, "get_Count", 0);
    if (!getCount) { error = std::string(label) + " count accessor missing"; return InventoryReadState::Failed; }

    const auto rCount = Invoke(getCount, rList.value, nullptr);
    if (!rCount.exc.empty()) { error = std::string(label) + " count invocation failed"; return InventoryReadState::Failed; }
    if (!rCount.value) return InventoryReadState::NotReady;
    void* unboxedCount = g_api.object_unbox(rCount.value);
    if (!unboxedCount) return InventoryReadState::NotReady;
    const int32_t rows = *static_cast<int32_t*>(unboxedCount);
    if (rows < 0) { error = std::string(label) + " count negative"; return InventoryReadState::Failed; }
    if (rows > kInventoryMaxRows) { error = std::string(label) + " count exceeds limit"; return InventoryReadState::Failed; }

    void* getItem = FindMethodInHierarchy(listClass, "get_Item", 1);
    if (!getItem) { error = std::string(label) + " item accessor missing"; return InventoryReadState::Failed; }

    std::ostringstream rowsOut;
    std::unordered_set<std::string> seenIds;
    rowsOut << "[";
    for (int32_t i = 0; i < rows; i++) {
        int32_t index = i;
        void* args[1] = { &index };
        const auto rItem = Invoke(getItem, rList.value, args);
        if (!rItem.exc.empty()) { error = std::string(label) + " item invocation failed"; return InventoryReadState::Failed; }
        if (!rItem.value) { error = std::string(label) + " item missing"; return InventoryReadState::Failed; }
        std::ostringstream row;
        std::string rowError;
        const bool ok = idolCards
            ? SerializeIdolCardRow(rItem.value, seenIds, row, rowError)
            : SerializeSupportCardRow(rItem.value, seenIds, row, rowError);
        if (!ok) { error = rowError; return InventoryReadState::Failed; }
        rowsOut << (i ? "," : "") << row.str();
    }
    rowsOut << "]";
    json  = rowsOut.str();
    count = static_cast<int>(rows);
    return InventoryReadState::Ready;
}

// ---- sidecar publication ----------------------------------------------------

struct InventoryPublishOutcome {
    bool        accepted  = false;
    bool        completed = false;
    std::string status;
    std::string message;
};

// Removes ONLY the three files this run just wrote inside the temp directory and
// then removes that (newly created) empty directory. Never recursive and never
// touches a pre-existing snapshot directory.
void InventoryCleanupTemp(const std::wstring& tempDir, bool created) {
    if (!created) return;
    DeleteFileW((tempDir + L"\\idol-cards.json").c_str());
    DeleteFileW((tempDir + L"\\support-cards.json").c_str());
    DeleteFileW((tempDir + L"\\manifest.json").c_str());
    RemoveDirectoryW(tempDir.c_str());
}

// Reads a file back so a snapshot payload hash can be validated on disk before
// the directory becomes visible. Bounded by nothing but the file itself, which
// this run just wrote and whose size is bounded by the exporter limits.
std::string InventoryReadFileBytes(const std::wstring& path) {
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return std::string();
    std::string data;
    char buffer[4096];
    DWORD read = 0;
    while (ReadFile(file, buffer, static_cast<DWORD>(sizeof(buffer)), &read, nullptr) && read > 0) {
        data.append(buffer, static_cast<size_t>(read));
    }
    CloseHandle(file);
    return data;
}

// Serialized against the watchdog through the existing g_publishMutex, but this
// function never calls WriteResults. The first terminal inventory result wins
// because every terminal publication claims g_inventoryTerminalPublished while
// the mutex is held, and a success that lands after the diagnostic deadline is
// refused.
InventoryPublishOutcome PublishCardInventory(const std::string& requestedStatus,
                                             const std::string& requestedMessage,
                                             bool haveSnapshot,
                                             const std::string& idolJson,
                                             const std::string& supportJson,
                                             int idolCount,
                                             int supportCount,
                                             bool terminal) {
    InventoryPublishOutcome out;
    out.status  = requestedStatus;
    out.message = requestedMessage;
    try {
        std::lock_guard<std::mutex> publishLock(g_publishMutex);
        if (g_inventoryTerminalPublished.load(std::memory_order_acquire)) return out;

        const bool late = haveSnapshot && ElapsedSince() >= static_cast<long long>(kWatchdogMs);
        std::string status  = late ? std::string("timeout") : requestedStatus;
        std::string message = late ? std::string(kInventoryLateMessage) : requestedMessage;
        bool completed      = haveSnapshot && !late;

        std::wstring statusDir;
        std::string  snapshotRel;
        std::string  idolHash, supportHash;

        const std::wstring root = OutputDirectory();
        if (root.empty()) {
            status    = "io_error";
            message   = "output directory unavailable";
            completed = false;
        } else {
            const std::wstring inventoryDir = root + L"\\card-inventory";
            std::error_code ec;
            std::filesystem::create_directories(std::filesystem::path(inventoryDir), ec);
            if (ec) {
                status    = "io_error";
                message   = "inventory directory unavailable";
                completed = false;
            } else {
                statusDir = inventoryDir;
                if (completed) {
                    const std::wstring snapshotsDir = inventoryDir + L"\\snapshots";
                    std::filesystem::create_directories(std::filesystem::path(snapshotsDir), ec);
                    if (ec) {
                        status    = "io_error";
                        message   = "inventory snapshot directory unavailable";
                        completed = false;
                    } else {
                        const std::string snapshotId =
                            std::to_string(GetCurrentProcessId()) + "-" +
                            std::to_string(static_cast<unsigned long>(g_workerStartMs.load(std::memory_order_relaxed)));
                        const std::wstring finalDir = snapshotsDir + L"\\" + std::wstring(snapshotId.begin(), snapshotId.end());
                        const std::wstring tempDir  = finalDir + L".tmp";
                        std::error_code createError;
                        // A pre-existing temp directory is a collision failure: it
                        // is never reused, overwritten or deleted.
                        const bool tempCreated = std::filesystem::create_directory(std::filesystem::path(tempDir), createError);
                        if (!tempCreated || createError) {
                            status    = "io_error";
                            message   = "inventory temp directory collision";
                            completed = false;
                        } else {
                            idolHash    = SnapshotHash(idolJson);
                            supportHash = SnapshotHash(supportJson);
                            const std::wstring idolPath     = tempDir + L"\\idol-cards.json";
                            const std::wstring supportPath  = tempDir + L"\\support-cards.json";
                            const std::wstring manifestPath = tempDir + L"\\manifest.json";
                            bool ok = !idolHash.empty() && !supportHash.empty() &&
                                      WriteFileBytes(idolPath, idolJson) &&
                                      WriteFileBytes(supportPath, supportJson);
                            if (ok) {
                                // manifest is written last and only references the
                                // relative basenames plus their sha256.
                                const std::string manifest =
                                    std::string("{\"schema_version\":1,\"plugin_version\":\"") + kInventoryPluginVersion +
                                    "\",\"snapshot_id\":\"" + snapshotId +
                                    "\",\"pid\":" + std::to_string(GetCurrentProcessId()) +
                                    ",\"counts\":{\"idol_cards\":" + std::to_string(idolCount) +
                                    ",\"support_cards\":" + std::to_string(supportCount) + "}" +
                                    ",\"files\":{\"idol-cards.json\":\"" + idolHash +
                                    "\",\"support-cards.json\":\"" + supportHash + "\"}" +
                                    ",\"completed\":true,\"status\":\"completed\"}";
                                ok = WriteFileBytes(manifestPath, manifest);
                            }
                            // Validate the bytes actually on disk before the
                            // snapshot directory becomes visible.
                            if (ok) {
                                ok = SnapshotHash(InventoryReadFileBytes(idolPath)) == idolHash &&
                                     SnapshotHash(InventoryReadFileBytes(supportPath)) == supportHash;
                            }
                            if (ok) {
                                // No replace flag: an existing snapshot name makes
                                // the rename fail instead of clobbering old data.
                                if (MoveFileExW(tempDir.c_str(), finalDir.c_str(), 0)) {
                                    snapshotRel = "snapshots/" + snapshotId;
                                } else {
                                    status    = "io_error";
                                    message   = "inventory snapshot rename failed";
                                    completed = false;
                                    InventoryCleanupTemp(tempDir, true);
                                }
                            } else {
                                status    = "io_error";
                                message   = "inventory snapshot write failed";
                                completed = false;
                                InventoryCleanupTemp(tempDir, true);
                            }
                        }
                    }
                }
            }
        }

        // An incomplete status must not claim counts it cannot know (a failure
        // does not mean zero cards), so counts are null unless committed.
        const std::string counts = completed
            ? "{\"idol_cards\":" + std::to_string(idolCount) + ",\"support_cards\":" + std::to_string(supportCount) + "}"
            : std::string("{\"idol_cards\":null,\"support_cards\":null}");
        const std::string files = completed
            ? "{\"idol-cards.json\":\"" + idolHash + "\",\"support-cards.json\":\"" + supportHash + "\"}"
            : std::string("{}");
        const std::string statusJson =
            std::string("{\"schema_version\":1,\"plugin_version\":\"") + kInventoryPluginVersion +
            "\",\"status\":\"" + JsonEscape(status) +
            "\",\"message\":\"" + JsonEscape(message) +
            "\",\"completed\":" + (completed ? "true" : "false") +
            ",\"counts\":" + counts +
            ",\"files\":" + files +
            ",\"snapshot_dir\":\"" + JsonEscape(snapshotRel) +
            "\",\"elapsed_ms\":" + std::to_string(ElapsedSince()) +
            ",\"pid\":" + std::to_string(GetCurrentProcessId()) + "}";

        if (!statusDir.empty()) {
            const std::wstring tmpStatus   = statusDir + L"\\status.json.tmp";
            const std::wstring finalStatus = statusDir + L"\\status.json";
            const bool written = WriteFileBytes(tmpStatus, statusJson) &&
                                 MoveFileExW(tmpStatus.c_str(), finalStatus.c_str(), MOVEFILE_REPLACE_EXISTING) != FALSE;
            if (!written) {
                DeleteFileW(tmpStatus.c_str());
                // The on-disk status may now be stale; record the truthful disk
                // outcome in the returned result and make one best-effort
                // incomplete status write (never claiming completion).
                status    = "io_error";
                message   = "inventory status write failed";
                completed = false;
                const std::string fallback =
                    std::string("{\"schema_version\":1,\"plugin_version\":\"") + kInventoryPluginVersion +
                    "\",\"status\":\"io_error\",\"message\":\"inventory status write failed\",\"completed\":false" +
                    ",\"counts\":{\"idol_cards\":null,\"support_cards\":null},\"files\":{},\"snapshot_dir\":\"\"" +
                    ",\"elapsed_ms\":" + std::to_string(ElapsedSince()) +
                    ",\"pid\":" + std::to_string(GetCurrentProcessId()) + "}";
                if (WriteFileBytes(tmpStatus, fallback))
                    MoveFileExW(tmpStatus.c_str(), finalStatus.c_str(), MOVEFILE_REPLACE_EXISTING);
            }
        }

        out.status    = status;
        out.message   = message;
        out.completed = completed;
        out.accepted  = true;
        if (terminal || late) g_inventoryTerminalPublished.store(true, std::memory_order_release);
        return out;
    } catch (...) {
        out.accepted  = true;
        out.completed = false;
        out.status    = "io_error";
        out.message   = "inventory publication failed";
        if (terminal) g_inventoryTerminalPublished.store(true, std::memory_order_release);
        return out;
    }
}

// ---- probe ------------------------------------------------------------------

InventoryExportResult InventoryTerminalFailure(const std::string& label) {
    InventoryExportResult out;
    out.terminal = true;
    const InventoryPublishOutcome outcome =
        PublishCardInventory("error", label, false, std::string(), std::string(), 0, 0, true);
    out.status    = outcome.accepted ? outcome.status : std::string("error");
    out.message   = outcome.accepted ? outcome.message : label;
    out.completed = false;
    return out;
}

InventoryExportResult InventoryPublishCompleted(const std::string& idolJson,
                                                const std::string& supportJson,
                                                int idolCount,
                                                int supportCount) {
    InventoryExportResult out;
    out.terminal     = true;
    out.idolCards    = idolCount;
    out.supportCards = supportCount;
    const InventoryPublishOutcome outcome =
        PublishCardInventory("completed", std::string(), true, idolJson, supportJson,
                             idolCount, supportCount, true);
    out.status    = outcome.accepted ? outcome.status : std::string("closed");
    out.message   = outcome.accepted ? outcome.message : std::string(kInventoryClosedMessage);
    out.completed = outcome.accepted && outcome.completed;
    return out;
}

InventoryExportResult InventoryClosedResult() {
    InventoryExportResult out;
    out.status   = "closed";
    out.message  = kInventoryClosedMessage;
    out.terminal = true;
    return out;
}

InventoryExportResult ProbeCardInventoryImpl(void* manager, void* managerClass) {
    InventoryExportResult pending;
    pending.status   = "running";
    pending.message  = kInventoryPendingMessage;
    pending.terminal = false;
    if (!manager || !managerClass) return pending;

    // Both full arrays are read every probe; ids are deduped per list.
    std::string idolJson, supportJson, idolError, supportError;
    int idolCount = 0, supportCount = 0;
    const InventoryReadState idolState = InventoryReadList(
        manager, managerClass, "get_UserIdolCardList", "get__userIdolCardList",
        "idol-cards", true, idolJson, idolCount, idolError);
    const InventoryReadState supportState = InventoryReadList(
        manager, managerClass, "get_UserSupportCardList", "get__userSupportCardList",
        "support-cards", false, supportJson, supportCount, supportError);

    if (idolState == InventoryReadState::Failed) return InventoryTerminalFailure(idolError);
    if (supportState == InventoryReadState::Failed) return InventoryTerminalFailure(supportError);
    if (idolState == InventoryReadState::NotReady || supportState == InventoryReadState::NotReady) {
        g_inventoryCandidate.valid = false;
        return pending;
    }

    // Account data readiness: the role list must be populated. Both lists being
    // empty during login is not a valid empty account, so keep waiting. A zero
    // support count is valid once the role list is non-empty.
    if (idolCount <= 0) {
        g_inventoryCandidate.valid = false;
        return pending;
    }

    const DWORD now = GetTickCount();
    if (g_inventoryCandidate.valid &&
        g_inventoryCandidate.idolJson == idolJson &&
        g_inventoryCandidate.supportJson == supportJson) {
        // Two consecutive complete serialized pair snapshots that are identical
        // and separated by at least kInventoryStableGapMs are a stable pair.
        if (static_cast<DWORD>(now - g_inventoryCandidate.tick) >= kInventoryStableGapMs)
            return InventoryPublishCompleted(idolJson, supportJson, idolCount, supportCount);
        return pending;
    }

    g_inventoryCandidate.valid       = true;
    g_inventoryCandidate.idolJson    = idolJson;
    g_inventoryCandidate.supportJson = supportJson;
    g_inventoryCandidate.tick        = now;
    return pending;
}

// At most ONE pair read, no Sleep and no loop: safe to call from the existing
// memory readiness polling. Ordinary inventory exceptions are contained here so
// the memory export can always continue.
InventoryExportResult ProbeCardInventory(void* manager, void* managerClass) {
    try {
        if (g_inventoryTerminalPublished.load(std::memory_order_acquire))
            return InventoryClosedResult();
        return ProbeCardInventoryImpl(manager, managerClass);
    } catch (...) {
        return InventoryTerminalFailure("inventory probe exception");
    }
}

// Bounded finish loop used only after the memory result has already been
// published: it consumes just the remaining cooperative budget, sleeps between
// probes (never inside a single probe) and stops immediately on a terminal
// inventory result or on a result already published by the watchdog.
InventoryExportResult FinishCardInventory(void* manager, void* managerClass) {
    InventoryExportResult last;
    last.status  = "running";
    last.message = kInventoryPendingMessage;
    if (g_inventoryTerminalPublished.load(std::memory_order_acquire)) return InventoryClosedResult();
    if (!manager || !managerClass) return last;
    const DWORD start = g_workerStartMs.load(std::memory_order_relaxed);
    if (!start) return last;
    while (static_cast<DWORD>(GetTickCount() - start) < kMaxWaitMs) {
        if (g_inventoryTerminalPublished.load(std::memory_order_acquire)) return InventoryClosedResult();
        last = ProbeCardInventory(manager, managerClass);
        if (last.terminal) return last;
        Sleep(kPollMs);
    }
    const InventoryPublishOutcome outcome = PublishCardInventory(
        "timeout", kInventoryTimeoutMessage, false, std::string(), std::string(), 0, 0, true);
    last.status = outcome.accepted ? outcome.status : std::string("closed");
    last.message = outcome.accepted ? outcome.message : std::string(kInventoryClosedMessage);
    last.completed = false;
    last.terminal = true;
    return last;
}

// ---- lifecycle hooks --------------------------------------------------------
//
// StartCardInventoryStatus publishes the initial running status without touching
// any game API. FinishInventoryIfPending is called from the worker's DoneGuard on
// every exit path and must never overwrite an existing terminal inventory result.

void StartCardInventoryStatus() {
    PublishCardInventory("running", kInventoryPendingMessage, false, std::string(), std::string(), 0, 0, false);
}

void FinishInventoryIfPending() {
    if (g_inventoryTerminalPublished.load(std::memory_order_acquire)) return;
    PublishCardInventory("not_loaded", kInventoryUnfinishedMessage,
                         false, std::string(), std::string(), 0, 0, true);
}

void PublishCardInventoryTimeout() {
    if (g_inventoryTerminalPublished.load(std::memory_order_acquire)) return;
    PublishCardInventory("timeout", kInventoryTimeoutMessage,
                         false, std::string(), std::string(), 0, 0, true);
}

void PublishCardInventoryNotStarted() {
    if (g_inventoryTerminalPublished.load(std::memory_order_acquire)) return;
    PublishCardInventory("error", kInventoryNotStartedMessage,
                         false, std::string(), std::string(), 0, 0, true);
}
