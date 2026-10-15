async function readLatestMemories() {
  const button = document.querySelector('#native-import');
  button.disabled = true;
  try {
    const result = await api('/api/native/import', {});
    catalog = await api('/api/bootstrap');
    for (const id of ['#library', '#search-library']) {
      document.querySelector(id).replaceChildren(...catalog.libraries.map(key => {
        const option = el('option', libraryLabel(key)); option.value = key; return option;
      }));
    }
    desktopLibrary = null;
    try { sessionStorage.removeItem('gk.desktopLibrary'); } catch {}
    chooseLibrary(result.library);
    groupId = null; groupData = null;
    await checkDesktopLibrary(); updateStartContext(); await refreshSeasonNav();
    notice(`已读取 ${result.total} 份：Stage 可用 ${result.ready} 份，Produce / 无舞台数据跳过 ${result.skipped_count} 份，转换失败 ${result.rejected_count} 份。`);
  } catch (error) { notice(error.message); }
  finally { button.disabled = false; }
}
function initNativeImport() {
  document.querySelector('#native-import').onclick = readLatestMemories;
}
if (window.gkReady) initNativeImport();
else window.addEventListener('gk-ready', initNativeImport, {once: true});
