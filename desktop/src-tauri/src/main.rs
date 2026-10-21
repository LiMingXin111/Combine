#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::Write;
use std::process::{Command, Stdio};
use tauri::{Manager, menu::{Menu, MenuItem}, tray::TrayIconBuilder};
use serde_json::{json, Value};
use std::sync::atomic::{AtomicU64, Ordering};
use tauri_plugin_dialog::DialogExt;
#[cfg(windows)]
use std::os::windows::process::CommandExt;

// The entire native process tree inherits this job. A crash or explicit quit
// closes its sole handle and terminates brokers, workers and model processes.
#[cfg(windows)]
struct ProcessLifetime(windows_sys::Win32::Foundation::HANDLE);
#[cfg(windows)]
impl ProcessLifetime {
    fn new() -> Result<Self, std::io::Error> {
        use windows_sys::Win32::System::{JobObjects::*, Threading::GetCurrentProcess};
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() { return Err(std::io::Error::last_os_error()); }
            let job = Self(handle);
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(handle, JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _, std::mem::size_of_val(&limits) as u32) == 0
                || AssignProcessToJobObject(handle, GetCurrentProcess()) == 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(job)
        }
    }
}
#[cfg(windows)]
impl Drop for ProcessLifetime {
    fn drop(&mut self) { unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0); } }
}

#[tauri::command]
async fn desktop_rpc(app: tauri::AppHandle, method: String, params: Value, request_id: Option<String>) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if method.len() > 80 || !params.is_object() { return Err("Invalid local request.".to_string()); }
        let resources = if cfg!(debug_assertions) {
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources")
        } else { app.path().resource_dir().map_err(|e| e.to_string())?.join("resources") };
        // Even a plain `tauri dev` cannot open the installed application's data.
        let development = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.dev");
        let root = if cfg!(debug_assertions) { development.join("native-workspace") }
            else { app.path().app_local_data_dir().map_err(|e| e.to_string())?.join("workspace") };
        std::fs::create_dir_all(&root).map_err(|e| e.to_string())?;
        let python = resources.join("python/python.exe");
        let runtime = if cfg!(debug_assertions) {
            let manifest = development.join("runtime.json");
            if manifest.exists() {
                let data: Value = serde_json::from_slice(&std::fs::read(manifest).map_err(|_| "Cannot read the development runtime revision.")?)
                    .map_err(|_| "Invalid development runtime manifest.")?;
                let revision = data["revision"].as_str().ok_or("Missing development revision.")?;
                if revision.len() != 64 || !revision.bytes().all(|b| b.is_ascii_hexdigit()) { return Err("Invalid development revision.".into()); }
                resources.join(format!("runtime-dev-{}", revision))
            } else { resources.join("runtime") }
        } else { resources.join("runtime") };
        let launcher = runtime.join("launcher.py");
        if !python.is_file() || !launcher.is_file() { return Err("The packaged local runtime is missing. Rebuild the desktop bundle.".into()); }
        let mut command = Command::new(python);
        command.arg("-I").arg(launcher).arg("--root").arg(root)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        command.env_remove("COMBINE_DEV_TRACE");
        if cfg!(debug_assertions) { command.env("COMBINE_DEV_TRACE", development.join("logs/rpc.jsonl")); }
        #[cfg(windows)] command.creation_flags(0x08000000);
        let mut child = command.spawn().map_err(|e| e.to_string())?;
        static REQUEST_SEQUENCE: AtomicU64 = AtomicU64::new(1);
        let id = request_id.filter(|s| s.len() == 36 && s.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-'))
            .unwrap_or_else(|| format!("desktop-{}", REQUEST_SEQUENCE.fetch_add(1, Ordering::Relaxed)));
        let payload = serde_json::to_vec(&json!({"id": id, "method": method, "params": params})).map_err(|e| e.to_string())?;
        child.stdin.take().ok_or("Cannot open the local request channel.")?.write_all(&payload).map_err(|e| e.to_string())?;
        let output = child.wait_with_output().map_err(|e| e.to_string())?;
        if !output.status.success() { return Err("The local broker could not start. Check the packaged runtime.".into()); }
        serde_json::from_slice(&output.stdout).map_err(|_| "The local broker returned an invalid response.".to_string())
    }).await.map_err(|e| e.to_string())?
}

#[tauri::command]
async fn save_export(app: tauri::AppHandle, filename: String, content: String) -> Result<bool, String> {
    if !filename.ends_with(".md") && !filename.ends_with(".csv") && !filename.ends_with(".txt") { return Err("Choose a text export format.".into()); }
    tauri::async_runtime::spawn_blocking(move || {
        let target = app.dialog().file().set_file_name(&filename).blocking_save_file();
        match target {
            Some(path) => { let path = path.into_path().map_err(|e| e.to_string())?; std::fs::write(path, content).map_err(|e| e.to_string())?; Ok(true) },
            None => Ok(false)
        }
    }).await.map_err(|e| e.to_string())?
}

fn notify_development_exit() {
    // Only an explicit user exit asks the development owner to stop. Tauri's
    // Rust rebuilds may kill/restart this process without stopping Vite.
    #[cfg(debug_assertions)]
    if let Ok(session) = std::env::var("COMBINE_DEV_SESSION_ID") {
        if session.len() == 36 && session.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-') {
            let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.dev/stop-request.json");
            let _ = std::fs::write(path, json!({"session": session}).to_string());
        }
    }
}

fn main() {
    #[cfg(windows)]
    let _lifetime = ProcessLifetime::new().expect("Combine could not protect its local process lifetime");
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![desktop_rpc, save_export])
        .setup(|app| {
            #[cfg(debug_assertions)]
            if let Some(window) = app.get_webview_window("main") { window.set_title("Combine · Development")?; }
            let open = MenuItem::with_id(app, "open", "Open Combine", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Combine", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &quit])?;
            let mut pixels = vec![0u8; 16 * 16 * 4];
            for pixel in pixels.chunks_mut(4) { pixel.copy_from_slice(&[121, 83, 210, 255]); }
            TrayIconBuilder::new().icon(tauri::image::Image::new_owned(pixels, 16, 16))
                .tooltip("Combine — local workspace").menu(&menu)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" => { if let Some(window) = app.get_webview_window("main") { let _ = window.show(); let _ = window.set_focus(); } },
                    "quit" => {
                        let app = app.clone();
                        tauri::async_runtime::spawn(async move {
                            let _ = desktop_rpc(app.clone(), "workspace.checkpoint".into(), json!({}), None).await;
                            notify_development_exit();
                            app.exit(0);
                        });
                    }, _ => {}
                }).build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                if cfg!(debug_assertions) {
                    let app = window.app_handle().clone();
                    tauri::async_runtime::spawn(async move {
                        let _ = desktop_rpc(app.clone(), "workspace.checkpoint".into(), json!({}), None).await;
                        notify_development_exit();
                        app.exit(0);
                    });
                } else { let _ = window.hide(); }
            }
        })
        .run(tauri::generate_context!())
        .expect("Combine could not start");
}
