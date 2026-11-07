// The bot's avatar, banner and bio in this server, with a preview laid out like its Discord profile.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { Icon, Loading, Notice, TextArea, confirmDialog, toast, toastError, useGuild } from '../ui.js';

const MAX_BYTES = 8 * 1024 * 1024;
const MIN_SIDE = 64;
const MAX_SIDE = 4096;
const TYPES = ['image/jpeg', 'image/png', 'image/gif'];
const BIO_MAX = 190;

// The server checks the file again. This only saves a wait for an upload that would be refused.
function checkFile(file) {
  return new Promise((resolve) => {
    if (!TYPES.includes(file.type)) { resolve('Use a JPG, PNG or GIF.'); return; }
    if (file.size > MAX_BYTES) { resolve('That file is over 8 MB.'); return; }
    const picture = new Image();
    const url = URL.createObjectURL(file);
    picture.onload = () => {
      URL.revokeObjectURL(url);
      if (picture.width < MIN_SIDE || picture.height < MIN_SIDE) resolve(`That image is ${picture.width}x${picture.height}. The smallest allowed is 64x64.`);
      else if (picture.width > MAX_SIDE || picture.height > MAX_SIDE) resolve(`That image is ${picture.width}x${picture.height}. The largest allowed is 4096x4096.`);
      else resolve(null);
    };
    picture.onerror = () => { URL.revokeObjectURL(url); resolve("That file isn't an image the browser can read."); };
    picture.src = url;
  });
}

export default function PersonalizePage() {
  const guild = useGuild();
  const [saved, setSaved] = useState(null);
  const [bio, setBio] = useState('');
  const [avatar, setAvatar] = useState(null);
  const [banner, setBanner] = useState(null);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState(null);
  const avatarInput = useRef(null);
  const bannerInput = useRef(null);

  const adopt = (state) => { setSaved(state); setBio(state.bio); setAvatar(null); setBanner(null); };
  useEffect(() => { api.get('/personalize').then(adopt).catch(setProblem); }, [guild.id]);
  useEffect(() => () => { if (avatar) URL.revokeObjectURL(avatar.url); }, [avatar]);
  useEffect(() => () => { if (banner) URL.revokeObjectURL(banner.url); }, [banner]);

  if (problem) return html`<${Notice} kind="bad">${problem.message}<//>`;
  if (!saved) return html`<${Loading} />`;

  const pick = async (event, set) => {
    const file = event.currentTarget.files[0];
    event.currentTarget.value = '';
    if (!file) return;
    const refusal = await checkFile(file);
    if (refusal) { toast(refusal, 'bad'); return; }
    set({ file, url: URL.createObjectURL(file) });
  };
  const changed = !!avatar || !!banner || bio !== saved.bio;
  const save = async () => {
    const body = new FormData();
    if (avatar) body.append('avatar', avatar.file);
    if (banner) body.append('banner', banner.file);
    if (bio !== saved.bio) body.append('bio', bio);
    setSaving(true);
    try {
      adopt(await api.upload('/personalize', body));
      toast('The bot profile is updated.');
      guild.reload();
    } catch (error) { toastError(error); } finally { setSaving(false); }
  };
  const reset = async () => {
    const sure = await confirmDialog({ title: 'Reset the profile?', body: "The bot goes back to its normal avatar, banner and bio in this server.", confirm: 'Reset', danger: true });
    if (!sure) return;
    setSaving(true);
    try {
      adopt(await api.del('/personalize'));
      toast('The bot profile is back to normal.');
      guild.reload();
    } catch (error) { toastError(error); } finally { setSaving(false); }
  };

  const avatarShown = avatar ? avatar.url : (saved.avatar || guild.me.avatar);
  const bannerShown = banner ? banner.url : saved.banner;
  return html`<div class="profile-page">
    <${Notice}>
      <div class="row"><${Icon} name="info" /><b>Make the bot your server's own</b></div>
      <p class="muted" style=${{ margin: '6px 0 0' }}>Members in this server see this avatar, banner and bio. Everywhere else the bot looks as it always does.</p>
      <ul class="muted small profile-rules">
        <li>Formats: JPG, PNG and GIF</li>
        <li>Largest file: 8 MB</li>
        <li>Best sizes: avatar 128x128 pixels, banner 680x240 pixels</li>
        <li>Smallest and largest: 64x64 to 4096x4096 pixels</li>
      </ul>
    <//>
    <div class="card">
      <div class="card-head"><span>Profile preview</span></div>
      <div class="profile-banner" style=${bannerShown ? { backgroundImage: `url("${bannerShown}")` } : null}>
        <button class="btn small profile-edit" disabled=${saving} title="Upload a banner: up to 8 MB, best at 680x240 pixels" onClick=${() => bannerInput.current.click()}><${Icon} name="pencil" size=${14} /> Edit banner</button>
      </div>
      <div class="profile-body">
        <button class="profile-avatar" disabled=${saving} aria-label="Upload an avatar" title="Upload an avatar: up to 8 MB, best at 128x128 pixels" onClick=${() => avatarInput.current.click()}>
          <img src=${avatarShown} alt="" />
          <span class="profile-avatar-edit"><${Icon} name="pencil" size=${18} /></span>
        </button>
        <h3 class="profile-name">${guild.me.nick || guild.me.name}<span class="dc-tag">APP</span></h3>
        <${TextArea} label="Bio" rows=${3} maxLength=${BIO_MAX} placeholder="What this bot does in your server" value=${bio} disabled=${saving} onChange=${setBio} />
      </div>
      <input ref=${avatarInput} class="hidden" type="file" accept=${TYPES.join(',')} aria-label="Avatar image" onChange=${(e) => pick(e, setAvatar)} />
      <input ref=${bannerInput} class="hidden" type="file" accept=${TYPES.join(',')} aria-label="Banner image" onChange=${(e) => pick(e, setBanner)} />
      <div class="card-foot profile-actions">
        <button class="btn primary block" disabled=${!changed || saving || bio.length > BIO_MAX} onClick=${save}>${saving ? 'Saving...' : 'Save changes'}</button>
        <button class="btn danger block" disabled=${saving || !saved.custom} onClick=${reset}>Reset</button>
      </div>
    </div>
  </div>`;
}
