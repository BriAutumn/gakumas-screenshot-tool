// Publishes the game master tables needed to rebuild the simulator ID mapping.
void* MasterCall(void* method, void* instance, void** arguments, const std::string& label) {
    if (!method) throw std::runtime_error(label + ": missing method");
    auto result = Invoke(method, instance, arguments);
    if (!result.exc.empty()) throw std::runtime_error(label + ": " + result.exc);
    return result.value;
}

void* MasterObject(void* method, void* instance, void** arguments, const std::string& label) {
    void* object = MasterCall(method, instance, arguments, label);
    if (!object) throw std::runtime_error(label + ": null result");
    return object;
}

void FinishMasterFile(std::ofstream& stream, const std::filesystem::path& temporary,
                     const std::filesystem::path& target) {
    stream.close();
    if (!stream || !MoveFileExW(temporary.c_str(), target.c_str(), MOVEFILE_REPLACE_EXISTING))
        throw std::runtime_error("cannot publish probe output");
}

void* CreateMasterFormatter(void* domain) {
    void* image = FindAssemblyImage(domain, "Google.Protobuf.dll");
    if (!image) throw std::runtime_error("Google.Protobuf.dll is not loaded");
    void* klass = g_api.class_from_name(image, "Google.Protobuf", "JsonFormatter");
    if (!klass) throw std::runtime_error("JsonFormatter class is missing");

    const auto assembly = GetModuleHandleW(L"GameAssembly.dll");
    using MethodGetParam = void* (*)(void*, uint32_t);
    auto getParam = reinterpret_cast<MethodGetParam>(GetProcAddress(assembly, "il2cpp_method_get_param"));
    using ObjectNew = void* (*)(void*);
    auto objectNew = reinterpret_cast<ObjectNew>(GetProcAddress(assembly, "il2cpp_object_new"));
    void* constructor = FindMethodInHierarchy(klass, ".ctor", 1);
    if (!getParam || !objectNew || !constructor) throw std::runtime_error("cannot resolve JsonFormatter constructor");
    void* settingsType = getParam(constructor, 0);
    void* settingsClass = settingsType ? g_api.class_from_type(settingsType) : nullptr;
    void* settings = settingsClass ? objectNew(settingsClass) : nullptr;
    if (!settings) throw std::runtime_error("cannot allocate JsonFormatter settings");
    bool enabled = true;
    void* settingArgs[] = {&enabled};
    MasterCall(FindMethodInHierarchy(settingsClass, ".ctor", 1), settings, settingArgs, "Settings constructor");

    void* formatter = objectNew(klass);
    if (!formatter) throw std::runtime_error("cannot allocate JsonFormatter");
    void* constructorArgs[] = {settings};
    MasterCall(constructor, formatter, constructorArgs, "JsonFormatter constructor");
    return formatter;
}

std::string ExportMasterTable(void* manager, void* formatter, const char* name,
                      const std::filesystem::path& directory) {
    const std::string getter = std::string("get_") + name + "Master";
    void* master = MasterObject(FindMethodInHierarchy(manager, getter.c_str(), 0), nullptr, nullptr, getter);
    int32_t order = 0;
    void* sortArgs[] = {&order};
    void* rows = MasterObject(FindMethodInHierarchy(g_api.object_get_class(master), "GetAllWithSortByKey", 1),
                             master, sortArgs, "GetAllWithSortByKey");
    void* listClass = g_api.object_get_class(rows);
    int64_t count = 0;
    std::string error;
    if (!ReadIntField(rows, GetField(listClass, "get_Count"), count, error) || count <= 0 || count > 100000)
        throw std::runtime_error("invalid master row count: " + error);
    void* getItem = FindMethodInHierarchy(listClass, "get_Item", 1);
    void* format = FindMethodInHierarchy(g_api.object_get_class(formatter), "Format", 1);

    const auto temporary = directory / (std::string(name) + ".tmp");
    std::ofstream stream(temporary, std::ios::binary);
    if (!stream) throw std::runtime_error("cannot open table output");
    stream << "[\n";
    for (int32_t index = 0; index < count; ++index) {
        void* itemArgs[] = {&index};
        void* row = MasterObject(getItem, rows, itemArgs, "master list item");
        void* formatArgs[] = {row};
        void* json = MasterObject(format, formatter, formatArgs, "format master row");
        const auto value = Utf16ToUtf8(g_api.string_chars(json), g_api.string_length(json));
        if (value.empty() || value.front() != '{' || value.back() != '}')
            throw std::runtime_error("formatter did not return a JSON object");
        if (index) stream << ",\n";
        stream << value;
        if (!stream) throw std::runtime_error("cannot write master row");
    }
    stream << "\n]\n";
    const auto target = directory / (std::string(name) + ".json");
    FinishMasterFile(stream, temporary, target);
    const auto length = std::filesystem::file_size(target);
    if (length > 128ull * 1024 * 1024) throw std::runtime_error("master table exceeds size limit");
    std::string bytes(static_cast<size_t>(length), '\0');
    std::ifstream input(target, std::ios::binary);
    if (!input.read(bytes.data(), static_cast<std::streamsize>(bytes.size())))
        throw std::runtime_error("cannot hash master table");
    auto digest = SnapshotHash(bytes);
    if (digest.empty()) throw std::runtime_error("cannot hash master table");
    return digest;
}

void ExportGameMasterSnapshot(void* domain) {
    std::filesystem::path staging;
    std::string table;
    try {
        const auto exportRoot = OutputDirectory();
        if (exportRoot.empty()) throw std::runtime_error("export directory is unavailable");
        const auto root = std::filesystem::path(exportRoot) / L"game-master";
        const auto snapshots = root / L"snapshots";
        std::filesystem::create_directories(snapshots);
        const auto token = std::to_string(GetCurrentProcessId()) + "-" + std::to_string(GetTickCount64());
        const auto candidate = snapshots / (".stage-" + token);
        if (!std::filesystem::create_directory(candidate)) throw std::runtime_error("cannot create snapshot staging folder");
        staging = candidate;
        void* image = FindAssemblyImage(domain, "Assembly-CSharp.dll");
        if (!image) throw std::runtime_error("Assembly-CSharp.dll is not loaded");
        void* manager = g_api.class_from_name(image, "Campus.Common.Master", "MasterManager");
        if (!manager) throw std::runtime_error("MasterManager class is missing");
        void* formatter = CreateMasterFormatter(domain);
        std::string fingerprint;
        std::ostringstream files;
        for (const char* name : {"ProduceCard", "ProduceItem", "IdolCard", "ProduceCardCustomize",
                 "ProduceCardGrowEffect", "ProduceExamEffect", "ProduceDescriptionProduceCardGrowEffect",
                 "ProduceExamTrigger", "Character"}) {
            table = name;
            gkdiag::Log().Checkpoint("game_master_table", name);
            const auto digest = ExportMasterTable(manager, formatter, name, staging);
            const auto file = table + ".json";
            if (!fingerprint.empty()) files << ',';
            files << '"' << file << "\":\"" << digest << '"';
            fingerprint += file + " " + digest + "\n";
        }
        const auto snapshot = SnapshotHash(fingerprint);
        if (snapshot.empty()) throw std::runtime_error("cannot identify master snapshot");
        const auto manifest = "{\"schema_version\":1,\"snapshot\":\"" + snapshot +
                              "\",\"files\":{" + files.str() + "}}\n";
        {
            std::ofstream stream(staging / L"snapshot.json", std::ios::binary);
            stream << manifest;
            stream.close();
            if (!stream) throw std::runtime_error("cannot write snapshot manifest");
        }
        const auto target = snapshots / snapshot;
        if (!MoveFileExW(staging.c_str(), target.c_str(), 0)) {
            // A snapshot with this digest contains the same bytes. Replace each
            // file atomically so another export can also repair disk corruption.
            if (!std::filesystem::is_directory(target))
                throw std::runtime_error("cannot publish master snapshot");
            for (const auto& entry : std::filesystem::directory_iterator(staging)) {
                const auto final = target / entry.path().filename();
                if (!MoveFileExW(entry.path().c_str(), final.c_str(), MOVEFILE_REPLACE_EXISTING))
                    throw std::runtime_error("cannot refresh existing master snapshot");
            }
            std::filesystem::remove(staging);
        }
        staging.clear();
        const auto temporary = root / ("active-" + token + ".tmp");
        std::ofstream stream(temporary, std::ios::binary);
        stream << manifest;
        FinishMasterFile(stream, temporary, root / L"active.json");
        gkdiag::Log().Checkpoint("game_master_completed", snapshot.c_str());
    } catch (const std::exception& error) {
        gkdiag::Log().Checkpoint("game_master_failed", (table + " " + error.what()).c_str());
        if (!staging.empty()) {
            std::error_code ignored;
            std::filesystem::remove_all(staging, ignored);
        }
    }
}
