// Thin remote for the local app — this extension downloads nothing itself, it
// just hands a URL to http://127.0.0.1:7788 and reports back.

const API = 'http://127.0.0.1:7788';

const badge = (text, color = '#666') => {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
};

const notify = (title, message) =>
  chrome.notifications.create({
    type: 'basic',
    iconUrl: 'data:image/svg+xml;base64,' + btoa(
      '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="#141417"/></svg>'
    ),
    title,
    message,
  });

async function send(url, mode) {
  if (!url || !/^https?:/i.test(url)) {
    notify('nothing to download', 'that page has no link to work with.');
    return;
  }

  badge('…', '#555');
  let id;
  try {
    const r = await fetch(`${API}/api/download`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url, mode, quality: 'max', audioFormat: 'mp3' }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    id = data.id;
  } catch (e) {
    badge('!', '#c33');
    notify('app not running', 'start the app (start.bat), then try again.');
    return;
  }

  // poll until it finishes, showing rough progress on the toolbar icon
  for (;;) {
    await new Promise(r => setTimeout(r, 700));
    let job;
    try {
      job = await (await fetch(`${API}/api/job/${id}`)).json();
    } catch { badge('!', '#c33'); return; }

    if (job.state === 'downloading' && job.progress != null) {
      badge(`${Math.round(job.progress)}`, '#555');
    } else if (job.state === 'processing') {
      badge('···', '#555');
    } else if (job.state === 'done') {
      badge('✓', '#2a7');
      chrome.downloads.download({ url: `${API}/api/file/${id}`, filename: job.filename });
      setTimeout(() => badge(''), 4000);
      return;
    } else if (job.state === 'error') {
      badge('!', '#c33');
      notify('could not download', job.error || 'unknown error');
      setTimeout(() => badge(''), 6000);
      return;
    }
  }
}

chrome.action.onClicked.addListener(tab => send(tab.url, 'auto'));

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: 'dl-video', title: 'Download this (video)', contexts: ['page', 'link', 'video'] });
  chrome.contextMenus.create({ id: 'dl-audio', title: 'Download this (audio only)', contexts: ['page', 'link', 'video', 'audio'] });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const url = info.linkUrl || info.srcUrl || info.pageUrl || tab?.url;
  send(url, info.menuItemId === 'dl-audio' ? 'audio' : 'auto');
});
