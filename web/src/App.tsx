import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

type Profile = {
  id: string;
  name: string;
  endpoint: string;
  region: string;
  bucket: string;
  access_key_id: string;
  disable_ssl: boolean;
  created_at?: string;
  updated_at?: string;
};

type ProfileForm = Omit<Profile, "id" | "created_at" | "updated_at"> & {
  id?: string;
  secret_access_key: string;
  session_token: string;
};

type Connection = {
  id: string;
  label: string;
  bucket: string;
  endpoint: string;
  region: string;
  connected_at: string;
};

type S3Object = {
  key: string;
  size: number;
  last_modified: string | null;
  etag: string;
  storage_class: string | null;
};

type Listing = {
  prefix: string;
  recursive: boolean;
  prefixes: string[];
  objects: S3Object[];
  key_count: number;
  is_truncated: boolean;
  next_token: string | null;
};

type PrefixDeletePreview = {
  prefix: string;
  object_count: number;
  total_bytes: number;
  sample_keys: string[];
};

type PrefixDeleteResult = {
  prefix: string;
  requested_count: number;
  deleted_count: number;
  error_count: number;
  errors: Array<{ Key?: string; Code?: string; Message?: string }>;
};

type DownloadJob = {
  id: string;
  kind: "file" | "prefix";
  target: string;
  download_name: string;
  connection_id: string;
  connection_label: string;
  bucket: string;
  status: "queued" | "scanning" | "running" | "completed" | "failed" | "cancelled";
  total_objects: number;
  completed_objects: number;
  total_bytes: number;
  completed_bytes: number;
  progress: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  ready: boolean;
};

type Notice = { tone: "good" | "warn" | "danger"; title: string; message: string };

const blankForm: ProfileForm = {
  name: "New S3 connection",
  endpoint: "",
  region: "us-east-1",
  bucket: "",
  access_key_id: "",
  secret_access_key: "",
  session_token: "",
  disable_ssl: false,
};

const numberFormat = new Intl.NumberFormat("zh-CN");
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", "Content-Type": "application/json", ...init?.headers },
  });
  const payload = (await response.json().catch(() => null)) as
    | { error?: { message?: string } | string }
    | T
    | null;
  if (!response.ok) {
    const error = payload && typeof payload === "object" && "error" in payload ? payload.error : null;
    const message = typeof error === "string" ? error : error?.message;
    throw new Error(message ?? `API ${response.status}`);
  }
  return payload as T;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : dateFormat.format(parsed);
}

function basename(key: string): string {
  return key.split("/").filter(Boolean).at(-1) ?? key;
}

function App() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [form, setForm] = useState<ProfileForm>(blankForm);
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(null);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [listing, setListing] = useState<Listing | null>(null);
  const [prefix, setPrefix] = useState("");
  const [recursive, setRecursive] = useState(false);
  const [activeTab, setActiveTab] = useState<"browser" | "downloads">("browser");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [downloads, setDownloads] = useState<DownloadJob[]>([]);
  const [profilesLoading, setProfilesLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [listingBusy, setListingBusy] = useState(false);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const [deletingPrefix, setDeletingPrefix] = useState<string | null>(null);

  const loadDownloads = useCallback(async () => {
    try {
      const payload = await request<{ downloads: DownloadJob[] }>("/api/downloads");
      setDownloads(payload.downloads);
    } catch {
      // The connection panel already surfaces API availability; polling stays quiet.
    }
  }, []);

  useEffect(() => {
    void loadDownloads();
    const timer = window.setInterval(() => void loadDownloads(), 1_500);
    return () => window.clearInterval(timer);
  }, [loadDownloads]);

  const loadProfiles = useCallback(async () => {
    setProfilesLoading(true);
    try {
      const payload = await request<{ profiles: Profile[] }>("/api/profiles");
      setProfiles(payload.profiles);
    } catch (error) {
      setNotice({
        tone: "danger",
        title: "LOCAL API OFFLINE",
        message: error instanceof Error ? error.message : "无法读取本地配置档案",
      });
    } finally {
      setProfilesLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProfiles();
  }, [loadProfiles]);

  const runList = useCallback(
    async (
      connectionId: string,
      targetPrefix: string,
      targetRecursive: boolean,
      continuationToken: string | null = null,
      append = false,
    ) => {
      setListingBusy(true);
      try {
        const payload = await request<Listing>("/api/list", {
          method: "POST",
          body: JSON.stringify({
            connection_id: connectionId,
            prefix: targetPrefix,
            recursive: targetRecursive,
            continuation_token: continuationToken,
            limit: 200,
          }),
        });
        setListing((current) => {
          if (!append || !current) return payload;
          return {
            ...payload,
            prefixes: [...current.prefixes, ...payload.prefixes],
            objects: [...current.objects, ...payload.objects],
          };
        });
        setPrefix(payload.prefix);
        setRecursive(payload.recursive);
        setNotice(null);
      } catch (error) {
        setNotice({
          tone: "danger",
          title: "LIST QUERY FAILED",
          message: error instanceof Error ? error.message : "无法读取对象列表",
        });
      } finally {
        setListingBusy(false);
      }
    },
    [],
  );

  function updateForm<K extends keyof ProfileForm>(key: K, value: ProfileForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function chooseProfile(profile: Profile) {
    if (connection) {
      void request("/api/disconnect", { method: "POST", body: JSON.stringify({ connection_id: connection.id }) }).catch(() => undefined);
    }
    setSelectedProfileId(profile.id);
    setForm({ ...profile, secret_access_key: "", session_token: "" });
    setConnection(null);
    setListing(null);
    setActiveTab("browser");
    setNotice({
      tone: "warn",
      title: "CREDENTIALS REQUIRED",
      message: "已载入连接元数据；请输入 Secret Key 后建立连接。",
    });
  }

  function startNewProfile() {
    if (connection) {
      void request("/api/disconnect", { method: "POST", body: JSON.stringify({ connection_id: connection.id }) }).catch(() => undefined);
    }
    setSelectedProfileId(null);
    setForm({ ...blankForm });
    setConnection(null);
    setListing(null);
    setActiveTab("browser");
    setNotice(null);
  }

  async function saveProfile(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    setSaving(true);
    try {
      const payload = await request<{ profile: Profile }>("/api/profiles", {
        method: "POST",
        body: JSON.stringify({
          id: selectedProfileId,
          name: form.name,
          endpoint: form.endpoint,
          region: form.region,
          bucket: form.bucket,
          access_key_id: form.access_key_id,
          disable_ssl: form.disable_ssl,
        }),
      });
      setProfiles((current) => {
        const next = current.filter((profile) => profile.id !== payload.profile.id);
        return [payload.profile, ...next];
      });
      setSelectedProfileId(payload.profile.id);
      setForm((current) => ({ ...current, id: payload.profile.id }));
      setNotice({ tone: "good", title: "PROFILE SAVED", message: "连接元数据已保存，密钥不会写入配置文件。" });
    } catch (error) {
      setNotice({ tone: "danger", title: "PROFILE NOT SAVED", message: error instanceof Error ? error.message : "配置保存失败" });
    } finally {
      setSaving(false);
    }
  }

  async function deleteProfile(profile: Profile) {
    if (!window.confirm(`删除连接档案“${profile.name}”？`)) return;
    try {
      await request<{ deleted: boolean }>(`/api/profiles/${encodeURIComponent(profile.id)}`, { method: "DELETE" });
      setProfiles((current) => current.filter((item) => item.id !== profile.id));
      if (selectedProfileId === profile.id) startNewProfile();
      setNotice({ tone: "good", title: "PROFILE REMOVED", message: "本地连接档案已删除。" });
    } catch (error) {
      setNotice({ tone: "danger", title: "DELETE FAILED", message: error instanceof Error ? error.message : "档案删除失败" });
    }
  }

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setConnecting(true);
    try {
      const payload = await request<{ connection: Connection }>("/api/connect", {
        method: "POST",
        body: JSON.stringify(form),
      });
      setConnection(payload.connection);
      setListing(null);
      setNotice({
        tone: "good",
        title: "CONNECTION ESTABLISHED",
        message: `${payload.connection.bucket} · HeadBucket verified`,
      });
      await runList(payload.connection.id, prefix, recursive);
    } catch (error) {
      setConnection(null);
      setListing(null);
      setNotice({ tone: "danger", title: "CONNECTION FAILED", message: error instanceof Error ? error.message : "S3 连接失败" });
    } finally {
      setConnecting(false);
    }
  }

  async function disconnect() {
    if (!connection) return;
    try {
      await request("/api/disconnect", { method: "POST", body: JSON.stringify({ connection_id: connection.id }) });
    } finally {
      setConnection(null);
      setListing(null);
      setActiveTab("browser");
      setNotice({ tone: "warn", title: "CONNECTION CLOSED", message: "凭证已从后端内存连接中释放。" });
    }
  }

  async function startDownload(kind: "file" | "prefix", target: string) {
    if (!connection) return;
    try {
      await request<{ download: DownloadJob }>("/api/downloads", {
        method: "POST",
        body: JSON.stringify({ connection_id: connection.id, kind, target }),
      });
      setNotice({
        tone: "good",
        title: kind === "file" ? "FILE DOWNLOAD QUEUED" : "ZIP BUILD QUEUED",
        message: kind === "file" ? basename(target) : `正在准备 ${basename(target)}.zip`,
      });
      setActiveTab("downloads");
      await loadDownloads();
    } catch (error) {
      setNotice({ tone: "danger", title: "DOWNLOAD NOT STARTED", message: error instanceof Error ? error.message : "下载任务创建失败" });
    }
  }

  async function cancelDownload(job: DownloadJob) {
    try {
      await request(`/api/downloads/${encodeURIComponent(job.id)}`, { method: "DELETE" });
      await loadDownloads();
    } catch (error) {
      setNotice({ tone: "danger", title: "CANCEL FAILED", message: error instanceof Error ? error.message : "无法取消下载" });
    }
  }

  async function deleteObject(item: S3Object) {
    if (!connection || deletingKey || deletingPrefix) return;
    const confirmed = window.confirm(
      `删除这个对象？\n\nBucket: ${connection.bucket}\nKey: ${item.key}\n\n此操作不可撤销。`,
    );
    if (!confirmed) return;
    setDeletingKey(item.key);
    try {
      await request<{ deleted: boolean }>("/api/objects", {
        method: "DELETE",
        body: JSON.stringify({ connection_id: connection.id, key: item.key }),
      });
      setListing((current) => {
        if (!current) return current;
        return {
          ...current,
          objects: current.objects.filter((object) => object.key !== item.key),
          key_count: Math.max(0, current.key_count - 1),
        };
      });
      setNotice({ tone: "good", title: "OBJECT DELETED", message: `${item.key} 已删除。` });
    } catch (error) {
      setNotice({ tone: "danger", title: "DELETE FAILED", message: error instanceof Error ? error.message : "对象删除失败" });
    } finally {
      setDeletingKey(null);
    }
  }

  async function deletePrefix(targetPrefix: string) {
    if (!connection || deletingKey || deletingPrefix) return;
    const visiblePrefix = prefix;
    const visibleRecursive = recursive;
    setDeletingPrefix(targetPrefix);
    try {
      const preview = await request<PrefixDeletePreview>("/api/prefix-delete-preview", {
        method: "POST",
        body: JSON.stringify({ connection_id: connection.id, prefix: targetPrefix }),
      });
      if (preview.object_count === 0) {
        setNotice({ tone: "warn", title: "PREFIX EMPTY", message: `${preview.prefix} 下没有可删除的对象。` });
        return;
      }
      const sample = preview.sample_keys.map((key) => `• ${key}`).join("\n");
      const more = preview.object_count > preview.sample_keys.length ? "\n…" : "";
      const confirmed = window.confirm(
        `递归删除这个目录？\n\nBucket: ${connection.bucket}\nPrefix: ${preview.prefix}\n对象数: ${numberFormat.format(preview.object_count)}\n总大小: ${formatBytes(preview.total_bytes)}\n\n示例 Key:\n${sample}${more}\n\n此操作不可撤销。`,
      );
      if (!confirmed) return;
      const result = await request<PrefixDeleteResult>("/api/prefixes", {
        method: "DELETE",
        body: JSON.stringify({ connection_id: connection.id, prefix: preview.prefix }),
      });
      await runList(connection.id, visiblePrefix, visibleRecursive);
      if (result.errors.length > 0) {
        setNotice({
          tone: "warn",
          title: "PREFIX PARTIALLY DELETED",
          message: `已删除 ${numberFormat.format(result.deleted_count)} 个对象，${numberFormat.format(result.error_count)} 个失败。`,
        });
      } else {
        setNotice({
          tone: "good",
          title: "PREFIX DELETED",
          message: `${result.prefix} 已删除 ${numberFormat.format(result.deleted_count)} 个对象。`,
        });
      }
    } catch (error) {
      setNotice({ tone: "danger", title: "PREFIX DELETE FAILED", message: error instanceof Error ? error.message : "目录删除失败" });
    } finally {
      setDeletingPrefix(null);
    }
  }

  function downloadReady(job: DownloadJob) {
    window.location.assign(`/api/downloads/${encodeURIComponent(job.id)}/file`);
  }

  function queryPrefix(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (connection) void runList(connection.id, prefix.trim(), recursive);
  }

  function openPrefix(targetPrefix: string) {
    if (!connection) return;
    setPrefix(targetPrefix);
    setRecursive(false);
    void runList(connection.id, targetPrefix, false);
  }

  function goHome() {
    if (!connection) return;
    setPrefix("");
    setRecursive(false);
    void runList(connection.id, "", false);
  }

  function copyKey(key: string) {
    void navigator.clipboard?.writeText(key);
    setNotice({ tone: "good", title: "KEY COPIED", message: "对象 Key 已复制到剪贴板。" });
  }

  const breadcrumbs = useMemo(() => {
    const segments = prefix.split("/").filter(Boolean);
    return segments.map((segment, index) => ({
      label: segment,
      value: `${segments.slice(0, index + 1).join("/")}/`,
    }));
  }, [prefix]);

  const objectCount = listing?.objects.length ?? 0;
  const folderCount = listing?.prefixes.length ?? 0;
  const currentDownloads = useMemo(
    () => (connection ? downloads.filter((job) => job.connection_id === connection.id) : []),
    [connection, downloads],
  );
  const backgroundDownloadCount = useMemo(
    () =>
      connection
        ? downloads.filter(
            (job) =>
              job.connection_id !== connection.id &&
              !["completed", "failed", "cancelled"].includes(job.status),
          ).length
        : 0,
    [connection, downloads],
  );

  return (
    <main className="console-shell">
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />
      <div className="grain" />

      <header className="topbar">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">⌁</span>
          <div>
            <p className="eyebrow">LOCAL OBJECT STORAGE CONSOLE</p>
            <h1>S3 / OBJECT INDEX</h1>
          </div>
        </div>
        <div className="topbar-meta">
          <span className={`status-chip ${connection ? "live" : "idle"}`}><i />{connection ? "CONNECTED" : "STANDBY"}</span>
          <span className="storage-label">CREDENTIALS STAY LOCAL</span>
        </div>
      </header>

      <section className="hero-panel reveal reveal-one">
        <div className="hero-copy">
          <p className="eyebrow">S3 CONNECTION DESK · PHASE 01</p>
          <h2>把对象存储<br /><em>变成一张地图。</em></h2>
          <p className="hero-message">管理连接档案，验证 Bucket，再沿着前缀向下浏览对象。</p>
        </div>
        <div className="hero-terminal" aria-label="当前连接摘要">
          <div className="terminal-top"><span><i /> <i /> <i /></span><b>SESSION / {connection ? "LIVE" : "IDLE"}</b></div>
          <div className="terminal-body">
            <span className="terminal-prompt">$ s3ctl</span>
            <strong>{connection ? "list --connected" : "await --credentials"}</strong>
            <small>{connection ? `${connection.bucket} · ${connection.region}` : "建立一个连接以开始查询"}</small>
          </div>
          <div className="terminal-footer"><span>HEAD BUCKET</span><strong>{connection ? "VERIFIED" : "—"}</strong></div>
        </div>
      </section>

      <section className="metric-strip reveal reveal-two" aria-label="当前状态">
        <Metric label="PROFILES" value={numberFormat.format(profiles.length)} detail="local metadata" accent="lime" />
        <Metric label="CONNECTION" value={connection ? "01" : "00"} detail={connection ? "active session" : "no active session"} accent="cyan" />
        <Metric label="FOLDERS" value={numberFormat.format(folderCount)} detail="current prefix" accent="amber" />
        <Metric label="OBJECTS" value={numberFormat.format(objectCount)} detail={listing?.is_truncated ? "first page" : "current view"} accent="paper" />
      </section>

      <section className="workbench-grid reveal reveal-three">
        <aside className="profile-panel panel">
          <div className="panel-heading">
            <div><p className="eyebrow">CONFIGURATION LEDGER</p><h3>连接档案</h3></div>
            <button className="icon-button" type="button" onClick={startNewProfile} title="新建连接档案">＋</button>
          </div>
          <p className="panel-intro">保存地址、区域与 Bucket 元数据。Secret Key 只在连接时使用，不落盘。</p>
          <div className="profile-list">
            {profilesLoading ? <div className="skeleton-list"><span /><span /><span /></div> : null}
            {!profilesLoading && profiles.length === 0 ? (
              <button className="empty-profile" type="button" onClick={startNewProfile}>
                <span className="empty-plus">＋</span>
                <strong>建立第一个连接</strong>
                <small>ADD YOUR ENDPOINT</small>
              </button>
            ) : null}
            {!profilesLoading && profiles.map((profile, index) => (
              <div className={`profile-row ${selectedProfileId === profile.id ? "is-selected" : ""}`} key={profile.id}>
                <button type="button" onClick={() => chooseProfile(profile)}>
                  <span className="profile-index">{String(index + 1).padStart(2, "0")}</span>
                  <span className="profile-main"><strong>{profile.name}</strong><small>{profile.bucket || "bucket not set"}</small></span>
                  <span className="profile-arrow">↗</span>
                </button>
                <button className="profile-delete" type="button" onClick={() => void deleteProfile(profile)} aria-label={`删除 ${profile.name}`}>×</button>
              </div>
            ))}
          </div>
          <div className="security-note"><span>◎</span><div><strong>LOCAL MEMORY ONLY</strong><p>已连接凭证只保留在本地服务进程内，断开后释放。</p></div></div>
        </aside>

        <div className="main-column">
          <form className="panel config-panel" onSubmit={connect}>
            <div className="panel-heading config-heading">
              <div><p className="eyebrow">ENDPOINT / AUTH / TARGET</p><h3>连接参数</h3></div>
              <span className="panel-count">{selectedProfileId ? "EDIT PROFILE" : "NEW PROFILE"}</span>
            </div>
            <div className="form-grid">
              <Field label="PROFILE NAME" value={form.name} onChange={(value) => updateForm("name", value)} placeholder="Research S3" />
              <Field label="ENDPOINT" wide value={form.endpoint} onChange={(value) => updateForm("endpoint", value)} placeholder="https://s3.example.internal" mono />
              <Field label="REGION" value={form.region} onChange={(value) => updateForm("region", value)} placeholder="us-east-1" />
              <Field label="BUCKET" value={form.bucket} onChange={(value) => updateForm("bucket", value)} placeholder="your-bucket" mono />
              <Field label="ACCESS KEY ID" value={form.access_key_id} onChange={(value) => updateForm("access_key_id", value)} placeholder="AKIA..." mono />
              <Field label="SECRET ACCESS KEY" type="password" value={form.secret_access_key} onChange={(value) => updateForm("secret_access_key", value)} placeholder={selectedProfileId ? "enter again to connect" : "required for connection"} mono />
              <Field label="SESSION TOKEN" type="password" value={form.session_token} onChange={(value) => updateForm("session_token", value)} placeholder="optional" mono />
              <label className="ssl-toggle"><input type="checkbox" checked={form.disable_ssl} onChange={(event) => updateForm("disable_ssl", event.target.checked)} /><span className="toggle-mark" /><span><strong>DISABLE SSL</strong><small>仅用于明确要求 HTTP 的可信端点</small></span></label>
            </div>
            <div className="config-actions">
              <button className="secondary-button" type="button" onClick={() => void saveProfile()} disabled={saving}>
                {saving ? "SAVING..." : "SAVE PROFILE"}<span>＋</span>
              </button>
              <button className="primary-button" type="submit" disabled={connecting}>
                {connecting ? "CONNECTING..." : connection ? "RECONNECT & LIST" : "CONNECT & TEST"}<span>↗</span>
              </button>
              {connection ? <button className="disconnect-button" type="button" onClick={() => void disconnect()}>DISCONNECT</button> : null}
            </div>
          </form>

          {notice ? <div className={`notice ${notice.tone}`}><span className="notice-dot" /><strong>{notice.title}</strong><span>{notice.message}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示">×</button></div> : null}

          <section className="panel browser-panel" aria-label="S3 对象列表">
            <div className="panel-heading browser-heading">
              <div><p className="eyebrow">{activeTab === "browser" ? "OBJECT BROWSER / READ ONLY" : "TRANSFER QUEUE / TEMPORARY DISK"}</p><h3>{activeTab === "browser" ? "前缀目录" : "下载任务"}</h3></div>
              {connection ? <span className="connected-label"><i />{connection.bucket}</span> : <span className="panel-count">NO SESSION</span>}
            </div>
            <nav className="workspace-tabs" aria-label="工作区菜单">
              <button className={activeTab === "browser" ? "is-active" : ""} type="button" onClick={() => setActiveTab("browser")}>PREFIX BROWSER <span>{folderCount + objectCount}</span></button>
              <button className={activeTab === "downloads" ? "is-active" : ""} type="button" onClick={() => setActiveTab("downloads")}>DOWNLOADS <span>{currentDownloads.length}</span></button>
            </nav>
            {activeTab === "downloads" ? (
              <div className="download-pane">
                {backgroundDownloadCount > 0 ? <div className="background-download-note"><span>↻</span><p><strong>{backgroundDownloadCount} 个任务</strong> 正在其他连接的后台继续传输</p></div> : null}
                {connection && currentDownloads.length > 0 ? <DownloadQueue jobs={currentDownloads.slice(0, 5)} onCancel={cancelDownload} onDownload={downloadReady} /> : <div className="browser-empty"><div className="empty-orbit"><span /><span /><b>↓</b></div><p className="eyebrow">NO DOWNLOADS IN THIS CONNECTION</p><strong>还没有下载任务</strong><small>在前缀目录中选择文件或目录，即可在这里查看进度。</small></div>}
              </div>
            ) : (
              <>
            <form className="browser-toolbar" onSubmit={queryPrefix}>
              <button className="home-button" type="button" onClick={goHome} disabled={!connection} title="返回根目录">⌂</button>
              <div className="breadcrumbs"><button type="button" onClick={goHome} disabled={!connection}>BUCKET</button>{breadcrumbs.map((crumb) => <span key={crumb.value}><b>/</b><button type="button" onClick={() => openPrefix(crumb.value)}>{crumb.label}</button></span>)}</div>
              <input value={prefix} onChange={(event) => setPrefix(event.target.value)} placeholder="输入前缀，例如 eng.libretexts.org/pdf/" disabled={!connection} spellCheck="false" />
              <label className="recursive-control"><input type="checkbox" checked={recursive} onChange={(event) => setRecursive(event.target.checked)} disabled={!connection} /><span />RECURSIVE</label>
              <button className="refresh-button" type="submit" disabled={!connection || listingBusy}>{listingBusy ? "..." : "QUERY ↗"}</button>
            </form>

            {!connection ? (
              <div className="browser-empty"><div className="empty-orbit"><span /><span /><b>⌁</b></div><p className="eyebrow">NO ACTIVE SESSION</p><strong>先建立一条连接</strong><small>填写 Endpoint、Bucket 和访问凭证，连接成功后这里会显示目录。</small></div>
            ) : listingBusy && !listing ? (
              <div className="browser-empty loading-state"><div className="loading-line" /><div className="loading-line short" /><small>正在读取对象索引…</small></div>
            ) : listing ? (
              <>
                <div className="listing-meta"><span><strong>{numberFormat.format(folderCount)}</strong> PREFIXES</span><span><strong>{numberFormat.format(objectCount)}</strong> OBJECTS</span><span className="listing-scope">{listing.recursive ? "RECURSIVE SCAN" : "DIRECT CHILDREN"}</span>{prefix.trim() ? <button className="prefix-delete-button" type="button" onClick={() => void deletePrefix(prefix)} disabled={deletingPrefix !== null || deletingKey !== null}>DELETE PREFIX ×</button> : null}</div>
                {folderCount > 0 ? <div className="folder-list">{listing.prefixes.map((folder) => <div className="folder-row" key={folder}><button className="folder-open" type="button" onClick={() => openPrefix(folder)}><span className="folder-icon">//</span><strong>{basename(folder)}</strong><small>{folder}</small><span>↗</span></button><span className="folder-actions"><button className="folder-download" type="button" onClick={() => void startDownload("prefix", folder)} title="打包下载目录">↓ ZIP</button><button className="folder-delete" type="button" onClick={() => void deletePrefix(folder)} title="递归删除目录" disabled={deletingPrefix !== null || deletingKey !== null}>×</button></span></div>)}</div> : null}
                {objectCount > 0 ? <div className="object-table"><div className="table-head"><span>OBJECT KEY</span><span>SIZE</span><span>MODIFIED</span><span>ACTIONS</span></div>{listing.objects.map((item) => <div className="object-row" key={item.key}><span className="object-name" title={item.key}><i />{basename(item.key)}<small>{item.key}</small></span><span>{formatBytes(item.size)}</span><span>{formatDate(item.last_modified)}</span><span className="object-actions"><button type="button" onClick={() => copyKey(item.key)} title="复制 Key">⧉</button><button type="button" onClick={() => void startDownload("file", item.key)} title="下载文件">↓</button><button className="object-delete" type="button" onClick={() => void deleteObject(item)} title="删除对象" disabled={deletingKey !== null}>×</button></span></div>)}</div> : null}
                {folderCount === 0 && objectCount === 0 ? <div className="no-results"><span>∅</span><strong>这个前缀下没有对象</strong><small>尝试返回上一级或调整查询路径。</small></div> : null}
                {listing.is_truncated && listing.next_token ? <button className="load-more" type="button" onClick={() => void runList(connection.id, prefix, recursive, listing.next_token, true)} disabled={listingBusy}>{listingBusy ? "LOADING..." : "LOAD NEXT PAGE →"}</button> : null}
              </>
            ) : null}
              </>
            )}
          </section>
        </div>
      </section>

      <footer className="footer-strip"><span>S3 / OBJECT INDEX · LOCAL CONTROL PLANE</span><span>{connection ? `CONNECTED · ${connection.region}` : "AWAITING CONNECTION"}</span><span>PHASE 02 · BROWSE + TRANSFER + DELETE</span></footer>
    </main>
  );
}

function Field({ label, value, onChange, placeholder, type = "text", wide = false, mono = false }: { label: string; value: string; onChange: (value: string) => void; placeholder: string; type?: string; wide?: boolean; mono?: boolean }) {
  return <label className={`field ${wide ? "wide" : ""}`}><span>{label}</span><input type={type} value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} className={mono ? "mono-input" : ""} autoComplete="off" /></label>;
}

function Metric({ label, value, detail, accent }: { label: string; value: string; detail: string; accent: string }) {
  return <article className={`metric-card ${accent}`}><p>{label}</p><strong>{value}</strong><span>{detail}</span></article>;
}

function DownloadQueue({ jobs, onCancel, onDownload }: { jobs: DownloadJob[]; onCancel: (job: DownloadJob) => void; onDownload: (job: DownloadJob) => void }) {
  return (
    <div className="download-queue-content" aria-label="下载任务">
      <div className="download-list">
        {jobs.map((job) => (
          <div className="download-row" key={job.id}>
            <div className="download-icon">{job.kind === "prefix" ? "ZIP" : "FILE"}</div>
            <div className="download-main">
              <strong title={job.target}>{job.download_name}</strong>
              <small title={job.target}>{job.target}</small>
              <div className="download-progress"><i style={{ width: `${job.progress}%` }} /></div>
            </div>
            <div className="download-stats">
              <b>{job.progress}%</b>
              <small>{job.total_objects ? `${job.completed_objects}/${job.total_objects} objects` : job.status.toUpperCase()}</small>
            </div>
            {job.ready ? <button className="download-action" type="button" onClick={() => onDownload(job)}>DOWNLOAD ↓</button> : job.status === "failed" ? <span className="download-failed" title={job.error ?? undefined}>FAILED</span> : job.status === "cancelled" ? <span className="download-cancelled">CANCELLED</span> : <button className="download-cancel" type="button" onClick={() => onCancel(job)}>CANCEL</button>}
          </div>
        ))}
      </div>
    </div>
  );
}

export default App;
