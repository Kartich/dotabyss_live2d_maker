const DATA_ROOT = './data/';
const PLAYER_NAME_URL = `${DATA_ROOT}player_name.txt`;
const R18_DATA_ROOT = './data_r18_all/';
const CHARA_DATA_ROOT = `${DATA_ROOT}chara/`;
const CHARA_EMOTION_ROOT = `${DATA_ROOT}emotion/charastand/`;
const EMOTION_KEYS = ["anger", "bepeevish", "dull", "elated", "exclamation", "exclaques", "flash", "flower", "heart", "imagination", "laugh", "panic", "question", "shock", "shy", "silence", "sleepy", "speak", "spirits", "sweat", "trouble", "twinkle"];
const BG_DATA_ROOT = `${DATA_ROOT}backgrounds/novel/`;
const LIVE2D_TAG = 'Live2DTag';
const L2D_DEFAULT_IDLE_MOTION = 'scene01_loop';

const L2D_GROUP_PRIORITY = [
  'Scene',
  'FaceEmotion',
  'FaceAngle',
  'EyeEmotion',
  'EyeOpen',
  'EyebrowEmotion',
  'MouthEmotion',
  'LipSync',
];
const L2D_LIP_SYNC = {
  layer: 'LipSync',
  mouthParameterPrefix: 'ParamMouthOpenY',
  suffixPattern: /^\d+$/,
  targetWaitSec: 0.06,
  blendModeSwitchTimeBeforeEnd: 0.3,
  lowFreqThreshold: 14700,
  midFreqThreshold: 29400,
  highFreqThreshold: 44100,
  lowFreqEnhancer: 1,
  midFreqEnhancer: 10,
  highFreqEnhancer: 100,
  maxMouthYSize: 150,
  webGLPeakLevelMultiplier: 7,
  maxOpening: 1,
  noiseFloorMin: 0.0025,
  noiseGateMargin: 0.0025,
  adaptivePeakFloor: 0.045,
  adaptivePeakDecaySec: 1.15,
  noiseFloorAdaptSec: 0.7,
  attackSec: 0.026,
  releaseSec: 0.072,
  tailReleaseSec: 0.105,
  endBlendSec: 0.24,
  endCloseSec: 0.11,
  syllableHoldSec: 0.018,
  consonantReleaseSec: 0.032,
  consonantCloseStrength: 0.48,
  voicedOpenBoost: 0.10,
  speechClosureBlend: 0.42,
  safeAuthoredDrive: 0.72,
  strongShapeDrive: 0.50,
  mouthTransitionGuardSec: 0.10,
  formDrive: 0.16,
  formAttackSec: 0.045,
  formReleaseSec: 0.075,
};
const AUTO_CHECK_MS = 100;
const AUTO_AFTER_VOICE_MS = 350;
const MOSAIC_PIXEL_DIV_X = 80;
const MOSAIC_PIXEL_DIV_Y = 45;
const MOSAIC_PADDING_PX = 2;
const LIVE2D_STAGE_SCALE_X = 1;
const SCENE_SHAKE_MAX_MS = 1800;


function safeStorageGet(key) {
  try { return window.localStorage?.getItem(key) || ''; } catch (_) { return ''; }
}

function safeStorageSet(key, value) {
  try { window.localStorage?.setItem(key, value); } catch (_) {}
}



// Compatibility and motion-processing support ported from the legacy player.
const CHARA_BLINK_MS = 140;
const JSON_FETCH_TIMEOUT_MS = 12000;
const LIVE2D_MODEL_LOAD_TIMEOUT_MS = 15000;
const LIVE2D_TEXTURE_COMPAT = {
  alphaEdgeFix: true,
  sampleStride: 8,
  suspectAlphaMin: 4,
  suspectAlphaMax: 96,
  suspectScore: 0.52,
  bleedAlphaMax: 104,
  bleedPasses: 1,
  unpremultiplyAlphaMin: 3,
  unpremultiplyAlphaMax: 250,
  unpremultiplyGate: 1.85,
  unpremultiplyBias: 18,
  minRendererResolution: 1.0,
  maxRendererResolution: 1.25,
  rendererPixelBudget: 2400000,
  mouthMaskBufferLow: 384,
  mouthMaskBufferHigh: 512,
};

function adaptiveVoiceMouthOpening(timeData, state, dt) {
  if (!timeData?.length) return 0;
  let sumSquares = 0;
  let peakAbs = 0;
  for (let i = 0; i < timeData.length; i += 1) {
    const sample = (Number(timeData[i]) - 128) / 128;
    const abs = Math.abs(sample);
    sumSquares += sample * sample;
    if (abs > peakAbs) peakAbs = abs;
  }
  const rms = Math.sqrt(sumSquares / timeData.length);
  const frameDt = clamp(Number(dt) || 0.016, 0.001, 0.08);

  let floor = Number(state.noiseFloor);
  if (!Number.isFinite(floor)) floor = L2D_LIP_SYNC.noiseFloorMin;
  let peak = Number(state.speechPeak);
  if (!Number.isFinite(peak)) peak = L2D_LIP_SYNC.adaptivePeakFloor;

  // Peak follows louder speech immediately and decays slowly, making quiet and
  // loud voice files produce similar mouth ranges without pumping every frame.
  const peakDecay = Math.exp(-frameDt / L2D_LIP_SYNC.adaptivePeakDecaySec);
  peak = Math.max(rms, L2D_LIP_SYNC.adaptivePeakFloor, peak * peakDecay);

  // Learn the recording's noise floor only on relatively quiet frames. This
  // prevents breaths / room noise from holding the mouth slightly open.
  if (rms < peak * 0.34 || rms < floor + 0.008) {
    const floorAlpha = 1 - Math.exp(-frameDt / L2D_LIP_SYNC.noiseFloorAdaptSec);
    const floorTarget = clamp(rms, L2D_LIP_SYNC.noiseFloorMin, 0.035);
    floor = lerp(floor, floorTarget, floorAlpha);
  }
  floor = clamp(floor, L2D_LIP_SYNC.noiseFloorMin, Math.max(L2D_LIP_SYNC.noiseFloorMin, peak * 0.55));

  const gateStart = floor + L2D_LIP_SYNC.noiseGateMargin;
  const gateWidth = Math.max(0.008, (peak - floor) * 0.16);
  const gate = smoothStep(clamp((rms - gateStart) / gateWidth, 0, 1));
  const dynamicSpan = Math.max(0.018, peak - floor);
  const normalized = clamp((rms - floor) / (dynamicSpan * 0.82), 0, 1);
  const absolute = clamp(rms * L2D_LIP_SYNC.webGLPeakLevelMultiplier, 0, 1);
  const crest = clamp((peakAbs - rms * 1.35) * 1.8, 0, 0.12);
  const opening = clamp((normalized * 0.72 + absolute * 0.28 + crest) * gate, 0, 1);

  state.noiseFloor = floor;
  state.speechPeak = peak;
  state.lastRms = rms;
  state.lastOpening = opening;
  return opening;
}

function analyzeVoiceSpectrum(frequencyData, sampleRate, fftSize, state, dt) {
  if (!frequencyData?.length || !Number.isFinite(sampleRate) || !Number.isFinite(fftSize) || fftSize <= 0) {
    return { voiced: Number(state.spectralVoiced) || 0, brightness: Number(state.spectralBrightness) || 0.35, consonant: 0 };
  }
  const binHz = sampleRate / fftSize;
  let low = 0;
  let mid = 0;
  let high = 0;
  for (let i = 1; i < frequencyData.length; i += 1) {
    const hz = i * binHz;
    if (hz < 80) continue;
    if (hz > 8000) break;
    const db = Number(frequencyData[i]);
    if (!Number.isFinite(db) || db <= -120) continue;
    const power = Math.pow(10, db / 10);
    if (hz < 700) low += power;
    else if (hz < 2600) mid += power;
    else high += power;
  }
  const total = low + mid + high;
  if (!(total > 1e-12)) {
    return { voiced: 0, brightness: Number(state.spectralBrightness) || 0.35, consonant: 0 };
  }
  const lowRatio = low / total;
  const midRatio = mid / total;
  const highRatio = high / total;
  const rawVoiced = clamp(lowRatio * 1.15 + midRatio * 0.62, 0, 1);
  const rawBrightness = clamp(highRatio * 1.35 + midRatio * 0.28, 0, 1);
  const rawConsonant = clamp((highRatio - 0.16) / 0.48, 0, 1) * clamp(1 - lowRatio * 0.72, 0, 1);
  const tau = 0.045;
  const alpha = 1 - Math.exp(-clamp(Number(dt) || 0.016, 0.001, 0.08) / tau);
  state.spectralVoiced = lerp(Number(state.spectralVoiced) || rawVoiced, rawVoiced, alpha);
  state.spectralBrightness = lerp(Number(state.spectralBrightness) || rawBrightness, rawBrightness, alpha);
  state.spectralConsonant = lerp(Number(state.spectralConsonant) || rawConsonant, rawConsonant, Math.min(1, alpha * 1.55));
  return {
    voiced: clamp(state.spectralVoiced, 0, 1),
    brightness: clamp(state.spectralBrightness, 0, 1),
    consonant: clamp(state.spectralConsonant, 0, 1),
  };
}

function createLive2DStraightAlphaCompatCanvas(source) {
  const { width, height } = live2dTextureSourceSize(source);
  if (!(width > 0 && height > 0)) return null;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) return null;
  ctx.clearRect(0, 0, width, height);
  ctx.drawImage(source, 0, 0, width, height);
  const image = ctx.getImageData(0, 0, width, height);
  const data = image.data;

  // Some exported Cubism atlases already contain RGB values that look
  // premultiplied in their semi-transparent edge pixels. PIXI/Cubism then
  // premultiplies the texture again on upload, producing dark blocky outlines
  // around small mouth ArtMeshes. Convert only pixels that match that signature
  // back to straight-alpha RGB; alpha and UVs are left untouched.
  const alphaMin = LIVE2D_TEXTURE_COMPAT.unpremultiplyAlphaMin;
  const alphaMax = LIVE2D_TEXTURE_COMPAT.unpremultiplyAlphaMax;
  const gate = LIVE2D_TEXTURE_COMPAT.unpremultiplyGate;
  const bias = LIVE2D_TEXTURE_COMPAT.unpremultiplyBias;
  for (let i = 0; i < data.length; i += 4) {
    const a = data[i + 3];
    if (a < alphaMin || a > alphaMax) continue;
    const r = data[i];
    const g = data[i + 1];
    const b = data[i + 2];
    const maxRgb = Math.max(r, g, b);
    if (maxRgb > a * gate + bias) continue;
    const gain = 255 / Math.max(1, a);
    data[i] = clamp(Math.round(r * gain), 0, 255);
    data[i + 1] = clamp(Math.round(g * gain), 0, 255);
    data[i + 2] = clamp(Math.round(b * gain), 0, 255);
  }

  // A tiny colour dilation for very-low-alpha edge pixels reduces atlas-island
  // stair stepping under bilinear sampling. We deliberately keep alpha exactly
  // as authored so masks and mouth silhouettes are not enlarged.
  const maxAlpha = LIVE2D_TEXTURE_COMPAT.bleedAlphaMax;
  for (let pass = 0; pass < LIVE2D_TEXTURE_COMPAT.bleedPasses; pass += 1) {
    const previous = new Uint8ClampedArray(data);
    for (let y = 1; y < height - 1; y += 1) {
      for (let x = 1; x < width - 1; x += 1) {
        const i = (y * width + x) * 4;
        const a = previous[i + 3];
        if (a <= 0 || a > maxAlpha) continue;
        let best = -1;
        let bestAlpha = a;
        const neighbors = [i - 4, i + 4, i - width * 4, i + width * 4];
        for (const n of neighbors) {
          const na = previous[n + 3];
          if (na > bestAlpha + 18) {
            bestAlpha = na;
            best = n;
          }
        }
        if (best < 0) continue;
        data[i] = previous[best];
        data[i + 1] = previous[best + 1];
        data[i + 2] = previous[best + 2];
      }
    }
  }

  ctx.putImageData(image, 0, 0);
  return canvas;
}

function isLive2DRuntimeResetName(value) {
  return /Reset(?:_N)?$/i.test(String(value || '').trim());
}

function live2dIdArray(source) {
  if (!source) return [];
  try {
    if (Array.isArray(source)) return source.map(live2dIdString).filter(Boolean);
    if (source instanceof Set) return Array.from(source, live2dIdString).filter(Boolean);
    if (typeof source.getSize === 'function' && typeof source.at === 'function') {
      const result = [];
      for (let i = 0; i < source.getSize(); i += 1) result.push(live2dIdString(source.at(i)));
      return result.filter(Boolean);
    }
    if (typeof source.length === 'number') return Array.from(source, live2dIdString).filter(Boolean);
  } catch (_) {}
  return [];
}

function live2dIdString(value) {
  if (value == null) return '';
  try {
    const inner = value.getString?.();
    if (inner?.s != null) return String(inner.s);
    if (inner != null) return String(inner);
  } catch (_) {}
  if (value.s != null) return String(value.s);
  return String(value);
}

function live2dModelCurveDefault(id) {
  if (id === 'eyeBlink' || id === 'opacity') return 1;
  return 0;
}

function live2dTexturePremultiplyEdgeScore(source) {
  const { width, height } = live2dTextureSourceSize(source);
  if (!(width > 0 && height > 0)) return 0;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) return 0;
  ctx.clearRect(0, 0, width, height);
  ctx.drawImage(source, 0, 0, width, height);
  const data = ctx.getImageData(0, 0, width, height).data;
  let semi = 0;
  let suspicious = 0;
  const stride = Math.max(1, Number(LIVE2D_TEXTURE_COMPAT.sampleStride) || 1);
  for (let i = 0; i < data.length; i += 4 * stride) {
    const a = data[i + 3];
    if (a < LIVE2D_TEXTURE_COMPAT.suspectAlphaMin || a > LIVE2D_TEXTURE_COMPAT.suspectAlphaMax) continue;
    semi += 1;
    const maxRgb = Math.max(data[i], data[i + 1], data[i + 2]);
    if (maxRgb <= a * 1.4 + 8) suspicious += 1;
  }
  return semi >= 64 ? suspicious / semi : 0;
}

function live2dTextureSourceSize(source) {
  const width = Number(source?.naturalWidth || source?.videoWidth || source?.width || 0);
  const height = Number(source?.naturalHeight || source?.videoHeight || source?.height || 0);
  return { width, height };
}

function normalizeLive2DModelCurveId(id) {
  const key = normalizeKey(id);
  if (key === 'eyeblink') return 'eyeBlink';
  if (key === 'lipsync') return 'lipSync';
  if (key === 'opacity' || key === 'modelopacity') return 'opacity';
  return key || '';
}

function promiseWithTimeout(promise, timeoutMs, message = 'operation timeout') {
  if (!(timeoutMs > 0)) return promise;
  let timer = 0;
  return Promise.race([
    Promise.resolve(promise),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), timeoutMs);
    }),
  ]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

function resolveLive2DGroup(group, motionName = '') {
  const rawGroup = String(group || '').trim();
  const rawName = String(motionName || '').trim();
  for (const candidate of [rawGroup, rawName]) {
    const key = normalizeKey(candidate);
    if (!key) continue;
    if (key === 'idle') return 'Scene';
    for (const known of L2D_GROUP_PRIORITY) {
      const knownKey = normalizeKey(known);
      if (key === knownKey || key.startsWith(knownKey)) return known;
    }
  }
  return rawGroup || rawName || 'default';
}

function stripLive2DResetSuffix(value) {
  return String(value || '').trim().replace(/Reset(?:_N)?$/i, '');
}

const el = {};
const app = {
  index: null,
  globalAudio: { cues: {} },
  emotionAssets: new Map(),
  backgroundAssets: new Map(),
  story: null,
  storyMeta: null,
  script: null,
  current: -1,
  running: false,
  auto: false,
  autoTimer: null,
  fastForward: false,
  fastForwardGeneration: 0,
  aspect169: false,
  language: (safeStorageGet("dotabyss-language") === "ja" ? "ja" : "cn"),
  playerName: "",
  runToken: 0,
  asyncToken: 0,
  labels: new Map(),
  registry: null,
  controller: null,
  characters: [],
  roomsByCharacter: new Map(),
  selectedCharacter: null,
};

document.addEventListener('DOMContentLoaded', async () => {
  bindElements();
  bindEvents();
  app.registry = buildCommandRegistry();
  app.controller = new NovelModelController();
  exposeManualReader();
  updateLanguageButtons();
  await loadPlayerName();
  await loadIndex();
});

function bindElements() {
  for (const id of [
    'characterView', 'roomView', 'playView', 'characterGrid', 'backToCharactersBtn',
    'languageToggleBtn', 'roomLanguageToggleBtn', 'playerNameBtn', 'roomPlayerNameBtn',
    'backToRoomsBtn', 'settingsBtn', 'settingsPanel', 'closeSettingsBtn', 'roomCharacterName', 'roomCount',
    'storyCount', 'storyList', 'storyTitle', 'storyMeta', 'stage', 'live2dCanvas',
    'bgLayer', 'mosaicLayer', 'fallbackTexture', 'sceneLayer', 'screenEffectLayer', 'transitionLayer', 'fadeLayer', 'motionBadge', 'statusLine',
    'messageBox', 'speaker', 'message', 'prevBtn', 'nextBtn', 'stepBtn', 'autoBtn', 'controls',
    'seek', 'position', 'commandList', 'modelInfo', 'stateInfo', 'fitBtn', 'aspect169Btn', 'reloadBtn', 'mosaicMode', 'showProgress', 'textOpacity', 'textOpacityTxt',
  ]) {
    el[id] = document.getElementById(id);
  }
}

function bindEvents() {
  el.backToCharactersBtn.addEventListener('click', () => showView('characters'));
  el.backToRoomsBtn.addEventListener('click', () => {
    stopPlayerSession();
    closeSettingsPanel();
    showView('rooms');
  });
  el.settingsBtn.addEventListener('click', () => toggleSettingsPanel());
  for (const button of [el.playerNameBtn, el.roomPlayerNameBtn]) button?.addEventListener('click', editPlayerName);
  for (const button of [el.languageToggleBtn, el.roomLanguageToggleBtn]) {
    button?.addEventListener('click', () => toggleLanguage());
  }
  el.closeSettingsBtn.addEventListener('click', () => closeSettingsPanel());
  el.nextBtn.addEventListener('click', () => { app.controller.sound.unlock(); nextText(); });
  el.prevBtn.addEventListener('click', () => { app.controller.sound.unlock(); previousText(); });
  el.stepBtn.addEventListener('click', () => { app.controller.sound.unlock(); stepCommand(); });
  el.autoBtn.addEventListener('click', () => { app.controller.sound.unlock(); toggleAuto(); });
  el.seek.addEventListener('input', () => jumpTo(Number(el.seek.value)));
  el.stage.addEventListener('click', () => { app.controller.sound.unlock(); nextText(); });
  el.showProgress.addEventListener('change', () => applyProgressVisibility(el.showProgress.checked));
  el.mosaicMode.addEventListener('change', () => {
    app.controller.live2d.setMosaicMode(el.mosaicMode.value);
    updateInspectors();
  });
  el.fitBtn.addEventListener('click', () => app.controller.live2d.fit());
  el.aspect169Btn.addEventListener('click', () => setAspect169Mode(!app.aspect169));
  el.reloadBtn.addEventListener('click', () => app.story && loadStory(app.story.id));
  el.textOpacity.addEventListener('input', () => applyTextOpacity(Number(el.textOpacity.value)));
  document.addEventListener('keydown', handleFastForwardKeyDown);
  document.addEventListener('keyup', handleFastForwardKeyUp);
  window.addEventListener('blur', () => stopFastForward());
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopFastForward();
  });
  document.addEventListener('pointerdown', () => app.controller.sound.unlock());
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });
  window.addEventListener('resize', () => app.controller.live2d.fit());
  applyProgressVisibility(el.showProgress.checked);
  applyTextOpacity(Number(el.textOpacity.value));
  updateAspect169Button();
}

function setAspect169Mode(enabled) {
  app.aspect169 = !!enabled;
  updateAspect169Button();
  app.controller?.live2d?.fit();
}

function updateAspect169Button() {
  if (!el.aspect169Btn) return;
  el.aspect169Btn.classList.toggle('active', app.aspect169);
  el.aspect169Btn.setAttribute('aria-pressed', String(app.aspect169));
  el.aspect169Btn.title = app.aspect169 ? '恢复原始画面' : '让 Live2D 等比铺满当前显示区域';
}
function applyProgressVisibility(visible) {
  el.controls.hidden = !visible;
}

function applyTextOpacity(value) {
  const numericValue = Number(value);
  const opacity = clamp(Number.isFinite(numericValue) ? numericValue : 0.82, 0, 1);
  document.documentElement.style.setProperty('--message-box-alpha', opacity.toFixed(2));
  document.documentElement.style.setProperty('--message-box-border-alpha', opacity <= 0.01 ? '0' : '0.14');
  document.documentElement.style.setProperty('--message-box-blur', opacity <= 0.01 ? '0px' : '6px');
  if (el.textOpacityTxt) el.textOpacityTxt.textContent = `${Math.round(opacity * 100)}%`;
}

async function loadIndex() {
  const primaryIndex = await fetchJson(`${DATA_ROOT}index.json`);
  const r18Index = await fetchJsonOptional(`${R18_DATA_ROOT}index.json`, { stories: [] });
  const characterIndex = await fetchJsonOptional(`${CHARA_DATA_ROOT}index.json`, { characters: [] });
  const storiesById = new Map();
  for (const source of [
    { root: DATA_ROOT, index: primaryIndex },
    { root: R18_DATA_ROOT, index: r18Index },
  ]) {
    for (const story of source.index.stories || []) {
      storiesById.set(String(story.id), { ...story, dataRoot: source.root });
    }
  }
  app.index = { ...primaryIndex, stories: Array.from(storiesById.values()) };
  buildCharacterRooms(characterIndex.characters || []);
  app.globalAudio = await loadGlobalAudioSources();
  app.emotionAssets = await loadEmotionAssetsFromParts();
  const backgroundIndex = await fetchJsonOptional(`${BG_DATA_ROOT}index.json`, { backgrounds: {} });
  app.backgroundAssets = new Map(Object.entries(backgroundIndex.backgrounds || {}).map(([key, item]) => [normalizeAssetKey(key), item]));
  el.storyCount.textContent = `${app.characters.length} characters / ${app.index.stories.length} rooms`;
  renderCharacterSelect();
  showView('characters');
}

async function loadEmotionAssetsFromParts() {
  const entries = await Promise.all(EMOTION_KEYS.map(async (id) => {
    const item = await fetchJsonOptional(`${CHARA_EMOTION_ROOT}parts/${id}/meta.json`, null);
    return item ? [normalizeAssetKey(id), item] : null;
  }));
  return new Map(entries.filter(Boolean));
}

function buildCharacterRooms(characterEntries) {
  const characterMap = new Map();
  for (const entry of characterEntries) {
    if (!entry?.body) continue;
    const variantId = String(entry.id || '').trim();
    const baseId = characterBaseId(variantId);
    if (!baseId) continue;
    const existing = characterMap.get(baseId);
    if (!existing || variantId.toUpperCase().endsWith('G')) {
      characterMap.set(baseId, {
        id: baseId,
        assetId: variantId.toLowerCase(),
        name: characterDisplayName(baseId),
        bodyPath: `${CHARA_DATA_ROOT}${variantId.toLowerCase()}/Body.png`,
        facePath: `${CHARA_DATA_ROOT}${variantId.toLowerCase()}/faces/normal.png`,
        metaPath: `${CHARA_DATA_ROOT}${variantId.toLowerCase()}/meta.json`,
      });
    }
  }
  app.roomsByCharacter = new Map();
  for (const story of app.index.stories) {
    const baseId = storyCharacterId(story);
    if (!baseId) continue;
    if (!app.roomsByCharacter.has(baseId)) app.roomsByCharacter.set(baseId, []);
    app.roomsByCharacter.get(baseId).push(story);
    if (!characterMap.has(baseId)) {
      characterMap.set(baseId, {
        id: baseId,
        assetId: `${baseId}g`,
        name: characterDisplayName(baseId),
        bodyPath: `${CHARA_DATA_ROOT}${baseId}g/Body.png`,
        facePath: `${CHARA_DATA_ROOT}${baseId}g/faces/normal.png`,
        metaPath: `${CHARA_DATA_ROOT}${baseId}g/meta.json`,
      });
    }
  }
  app.characters = Array.from(characterMap.values())
    .map((character) => ({
      ...character,
      rooms: app.roomsByCharacter.get(character.id) || [],
    }))
    .filter((character) => character.rooms.length > 0)
    .sort((a, b) => a.id.localeCompare(b.id));
  for (const rooms of app.roomsByCharacter.values()) {
    rooms.sort((a, b) => String(a.id).localeCompare(String(b.id)));
  }
}

async function loadGlobalAudioSources() {
  const sources = [];
  for (const root of [DATA_ROOT, R18_DATA_ROOT]) {
    for (const audioIndex of ['audio/se/index.json', 'audio/BGM/index.json']) {
      const index = await fetchJsonOptional(`${root}${audioIndex}`, null);
      if (index?.cues) sources.push({ basePath: root, cues: index.cues });
    }
  }
  return { sources };
}

async function loadPlayerName() {
  let fileName = '';
  try {
    const response = await fetch(PLAYER_NAME_URL, { cache: 'no-store' });
    if (response.ok) fileName = (await response.text()).trim();
  } catch (_) {}
  app.playerName = fileName || safeStorageGet('dotabyss-player-name') || '';
}

function editPlayerName() {
  const next = window.prompt('请输入主角名字：', app.playerName || '');
  if (next == null) return;
  const name = next.trim();
  app.playerName = name;
  safeStorageSet('dotabyss-player-name', name);
  fetch('/__player_name', { method: 'POST', headers: { 'Content-Type': 'text/plain; charset=utf-8' }, body: name }).catch(() => {});
}

function replacePlayerName(text) {
  return String(text || '').replace(/<user>/gi, app.playerName || '<user>');
}

function updateLanguageButtons() {
  const isCn = app.language !== 'ja';
  for (const button of [el.languageToggleBtn, el.roomLanguageToggleBtn]) {
    if (!button) continue;
    button.textContent = isCn ? '中文' : '日本語';
    button.title = isCn ? '当前为中文，点击切换日文' : '当前为日文，点击切换中文';
    button.setAttribute('aria-label', button.title);
  }
}

async function toggleLanguage() {
  app.language = app.language === 'ja' ? 'cn' : 'ja';
  safeStorageSet('dotabyss-language', app.language);
  updateLanguageButtons();
}

function chineseStoryUrl(meta) {
  const root = meta?.dataRoot || DATA_ROOT;
  if (root !== R18_DATA_ROOT) return null;
  const path = String(meta.path || '').replace(/^stories\//, 'stories_cn/');
  return `${root}${path}`;
}

async function loadStoryData(meta) {
  const japaneseUrl = `${meta.dataRoot || DATA_ROOT}${meta.path}`;
  if (app.language === 'cn') {
    const chineseUrl = chineseStoryUrl(meta);
    if (chineseUrl) {
      const chinese = await fetchJsonOptional(chineseUrl, null);
      if (chinese) return { data: chinese, root: R18_DATA_ROOT, path: String(meta.path).replace(/^stories\//, 'stories_cn/') };
    }
  }
  return { data: await fetchJson(japaneseUrl), root: meta.dataRoot || DATA_ROOT, path: meta.path };
}
async function loadStory(storyId) {
  const meta = app.index.stories.find((story) => story.id === storyId);
  if (!meta) return;
  showView('player');
  
  stopAuto();
 
  app.runToken += 1;
  app.asyncToken += 1;
  const currentAsyncToken = app.asyncToken; 

  if (app.controller) {
    app.controller.sound.stopAll();
    app.controller.live2d.stopLipSync();
    app.controller.async.reset();
    
  }
  app.storyMeta = meta;

  const loaded = await loadStoryData(meta);
  const storyData = loaded.data;
  app.storyDataRoot = loaded.root;
  app.storyDataPath = loaded.path;

  if (currentAsyncToken !== app.asyncToken) {
    return; 
  }
  app.story = storyData;
  app.script = app.story.scripts[0] || { commands: [], messages: [] };
  app.current = -1;
  app.running = false;
  app.labels = buildLabels(app.script.commands);
  
  const basePath = storyBasePath();
  app.controller.reset(app.story, basePath);
  app.controller.live2d.setMosaicMode(el.mosaicMode.value);
  await app.controller.live2d.loadStory(app.story, basePath);
  if (currentAsyncToken !== app.asyncToken) {
    return; 
  }
  app.controller.sound.loadStory(app.story, basePath);
  
  el.storyTitle.textContent = meta.title || meta.id;
  el.storyMeta.textContent = `${app.script.commands.length} commands / ${app.script.messages.length} messages`;
  el.seek.max = Math.max(0, app.script.commands.length - 1);
  el.seek.value = 0;
  
  renderStoryList();
  renderCommandList();
  updateInspectors();
  await nextText();
}

function buildLabels(commands) {
  const labels = new Map();
  for (const command of commands) {
    if (String(command.command).toLowerCase() === ':label') {
      const label = command.label || String(command.rawCommand || '').replace(/^:/, '') || command.args?.[0];
      if (label) labels.set(String(label).toLowerCase(), command.index);
    }
  }
  return labels;
}

function storyBasePath(meta = app.storyMeta) {
  // 中文模式只替换 story.json；Live2D、音频等资源继续从日文故事目录读取。
  const root = meta?.dataRoot || DATA_ROOT;
  const path = String(meta?.path || `stories/${app.story?.id || ''}/story.json`);
  const slash = path.lastIndexOf('/');
  return `${root}${slash >= 0 ? path.slice(0, slash + 1) : ''}`;
}

let thumbCache = null;

function showView(view) {
  const normalized = view === 'player' ? 'player' : view === 'rooms' ? 'rooms' : 'characters';
  document.querySelector('.reader-shell')?.setAttribute('data-view', normalized);
  if (normalized !== 'player') closeSettingsPanel();
  if (normalized === 'player') {
    requestAnimationFrame(() => app.controller.live2d.fit());
  }
}

function stopPlayerSession() {
  stopFastForward(false);
  stopAuto();
  app.runToken += 1;
  app.asyncToken += 1;
  app.running = false;
  app.controller?.reset(null, '');
}

function toggleSettingsPanel() {
  el.settingsPanel.hidden = !el.settingsPanel.hidden;
}

function closeSettingsPanel() {
  if (el.settingsPanel) el.settingsPanel.hidden = true;
}

function renderCharacterSelect() {
  el.characterGrid.innerHTML = '';
  for (const character of app.characters) {
    const item = document.createElement('button');
    item.className = 'character-card';
    item.type = 'button';
    item.innerHTML = `
      <span class="character-portrait-wrap">
        <span class="character-portrait-stage">
          <span class="character-portrait-body">
            <img class="character-portrait-body-image" src="${character.bodyPath}" alt="${escapeHtml(character.name)}" loading="lazy">
            <img class="character-portrait-face-image" src="${character.facePath}" alt="" loading="lazy">
          </span>
        </span>
      </span>
      <span class="character-name">${escapeHtml(character.name)}</span>
      <span class="character-meta">${character.rooms.length} rooms</span>
    `;
    hydrateCharacterPortrait(item, character);
    item.addEventListener('click', () => selectCharacter(character.id));
    el.characterGrid.appendChild(item);
  }
}

async function hydrateCharacterPortrait(card, character) {
  const wrap = card.querySelector('.character-portrait-wrap');
  const body = card.querySelector('.character-portrait-body');
  const face = card.querySelector('.character-portrait-face-image');
  try {
    const meta = await fetchJsonOptional(character.metaPath, null);
    if (!meta || !body || !face) return;
    applyPortraitRect(body, meta.bodyRect, 1024, 1600, 2048, 2048);
    applyPortraitFaceRect(face, meta.faceContentRect, meta.bodyRect);
  } catch (_) {
    wrap?.classList.add('missing');
  }
}

function applyPortraitRect(node, rect, fallbackWidth, fallbackHeight, rootWidth, rootHeight) {
  const size = rectSize(rect, fallbackWidth, fallbackHeight);
  const position = rectPosition(rect);
  const scale = rectScale(rect, 1);
  const pivot = rectPivot(rect);
  node.style.width = `${(size.x / rootWidth) * 100}%`;
  node.style.height = `${(size.y / rootHeight) * 100}%`;
  node.style.left = `${50 + (position.x / rootWidth) * 100}%`;
  node.style.top = `${50 - (position.y / rootHeight) * 100}%`;
  node.style.transform = `translate(${-pivot.x * 100}%, ${-(1 - pivot.y) * 100}%) scale(${scale.x}, ${scale.y})`;
}

function applyPortraitFaceRect(node, faceRect, bodyRect) {
  const bodySize = rectSize(bodyRect, 1024, 1600);
  const faceSize = rectSize(faceRect, 256, 256);
  const facePosition = rectPosition(faceRect);
  node.style.width = `${(faceSize.x / bodySize.x) * 100}%`;
  node.style.height = `${(faceSize.y / bodySize.y) * 100}%`;
  node.style.left = `${50 + (facePosition.x / bodySize.x) * 100}%`;
  node.style.top = `${50 - (facePosition.y / bodySize.y) * 100}%`;
}

function selectCharacter(characterId) {
  app.selectedCharacter = app.characters.find((character) => character.id === characterId) || null;
  if (!app.selectedCharacter) return;
  el.roomCharacterName.textContent = app.selectedCharacter.name;
  el.roomCount.textContent = `${app.selectedCharacter.rooms.length} rooms`;
  renderStoryList();
  showView('rooms');
}

async function renderStoryList() {
  el.storyList.innerHTML = '';
  if (!thumbCache) {
    try {
      const response = await fetch('data_r18_all/thumb.json');
      if (response.ok) {
        thumbCache = await response.json();
      } else {
        thumbCache = {};
      }
    } catch (e) {
      console.warn('未找到缩略图配置文件或加载失败，已启用纯文本兜底模式', e);
      thumbCache = {};
    }
  }
  const stories = app.selectedCharacter?.rooms || [];
  for (const story of stories) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = `story-item${app.story?.id === story.id ? ' active' : ''}`;
    
    const storyIdStr = String(story.id).trim();
    let imgHtml = '';
    if (storyIdStr.endsWith('2')) {
      const thumbSrc = thumbCache[storyIdStr];
      if (thumbSrc) {
        imgHtml = `<img class="story-thumb" src="${thumbSrc}" alt="L2D" onerror="this.style.display='none'">`;
      }
    }
    item.innerHTML = `
      ${imgHtml}
      <div class="story-item-text-wrapper">
        <div class="story-item-title">${escapeHtml(roomDisplayName(story))}</div>
        <div class="story-item-meta">${story.stats.messageCount} text / ${story.stats.motionCount || 0} motions / ${story.stats.audioCueCount || 0} audio</div>
      </div>
    `;
    
    item.addEventListener('click', () => loadStory(story.id));
    el.storyList.appendChild(item);
  }
}

function characterBaseId(value) {
  const match = String(value || '').trim().match(/^(\d{6})\d{3}/);
  return match ? `${match[1]}000` : '';
}

function storyCharacterId(story) {
  return characterBaseId(story?.id);
}

function characterDisplayName(baseId) {
  return `角色 ${baseId}`;
}

function roomDisplayName(story) {
  return story?.title || String(story?.id || '');
}

function renderCommandList() {
  el.commandList.innerHTML = '';
  for (const command of app.script.commands) {
    const item = document.createElement('div');
    item.className = 'command-item';
    item.dataset.index = command.index;
    item.innerHTML = `
      <div>${command.index.toString().padStart(4, '0')} ${escapeHtml(command.rawCommand || command.command)}</div>
      <div class="command-meta">${escapeHtml((command.args || []).join(' | '))}</div>
    `;
    item.addEventListener('click', () => jumpTo(command.index));
    el.commandList.appendChild(item);
  }
}

async function nextText() {
  if (app.running || !app.script) return;
  completeCurrentMessagePause();
  app.running = true;
  const token = app.runToken;
  
  try {
    while (app.current + 1 < app.script.commands.length && token === app.runToken) {
      const nextIndex = app.current + 1; 
      
      const result = await executeAt(nextIndex, token, false);
      if (token !== app.runToken) {
        return; 
      }
      if (result?.jumpIndex != null) {
        app.current = result.jumpIndex - 1;
        continue;
      }
      if (result?.pauseOnText) break;
    }
  } finally {
    if (token === app.runToken) {
      app.running = false;
      updatePosition();
      queueAuto();
    }
  }
}

async function stepCommand() {
  if (app.running || !app.script || app.current + 1 >= app.script.commands.length) return;
  completeCurrentMessagePause();
  app.running = true;
  const token = ++app.runToken;
  try {
    await executeAt(app.current + 1, token, false);
  } finally {
    app.running = false;
    updatePosition();
    queueAuto();
  }
}

async function executeAt(index, token, replaying, replayTargetIndex = -1) {
  const command = app.script.commands[index];
  app.current = index;
  const context = new NovelContext(app, app.controller, token, replaying, replayTargetIndex);
  const result = await app.registry.execute(context, command);
  app.controller.scene.render();
  updatePosition();
  updateInspectors();
  if (replaying) {
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  return result;
}

async function previousText() {
  if (!app.script) return;
  const textIndexes = app.script.commands
    .filter((command) => isTextPauseCommand(command.command))
    .map((command) => command.index)
    .filter((index) => index < app.current);
  const target = textIndexes.length ? textIndexes[textIndexes.length - 1] : 0;
  await replayTo(target);
}

async function jumpTo(index) {
  await replayTo(clamp(index, 0, app.script.commands.length - 1));
}

async function replayTo(index) {
  stopAuto();
  const token = ++app.runToken;
  app.asyncToken += 1;
  const currentAsyncToken = app.asyncToken;
  const basePath = storyBasePath();
  app.controller.reset(app.story, basePath);
  await app.controller.live2d.loadStory(app.story, basePath);
  if (currentAsyncToken !== app.asyncToken) return;
  app.controller.sound.loadStory(app.story, basePath);
  app.current = -1;
  for (let i = 0; i <= index; i += 1) {
    const isLastFrame = (i === index);
    const result = await executeAt(i, token, isLastFrame ? false : true, index);
    if (token !== app.runToken) return;
    if (result?.jumpIndex != null && result.jumpIndex <= index) i = result.jumpIndex - 1;
  }
  app.running = false; 
  await new Promise(resolve => setTimeout(resolve, 5));
  if (app.controller.scene && typeof app.controller.scene.render === 'function') {
    app.controller.scene.render();
  }
  const voiceCue = app.controller.message.state.voiceCue;
  
  try {
    app.controller.sound.stop('voice');
  } catch(e) {}

  if (voiceCue) {
    const voiceFactor = window.app?.config?.voiceVolume ?? 1.0;
    app.controller.sound.play(voiceCue, 'voice', 'voice', { volume: voiceFactor });

    const currentLockId = Date.now() + '_' + Math.random();
    app.controller.sound._activeVoiceLockId = currentLockId;

    // Register the active voice before starting lip-sync: playLipSync validates
    // this channel and otherwise immediately cancels its own state.
    window._liveChannels = window._liveChannels || {};
    window._liveChannels['voice'] = {
      type: 'voice',
      origVolume: 1.0,
      soundModel: app.controller.sound,
      voiceLockId: currentLockId
    };
    app.controller.live2d.playLipSync(voiceCue, true);
  } else {
    if (window._liveChannels) delete window._liveChannels['voice'];
    if (app.controller.sound) app.controller.sound._activeVoiceLockId = null;
  }
  queueAuto();
}

async function replayStoryForTest(storyId) {
  await loadStory(storyId);
  stopAuto();
  const token = ++app.runToken;
  app.asyncToken += 1;
  const basePath = storyBasePath();
  app.controller.reset(app.story, basePath);
  await app.controller.live2d.loadStory(app.story, basePath);
  app.controller.sound.loadStory(app.story, basePath);
  app.current = -1;
  for (let i = 0; i < app.script.commands.length; i += 1) {
    const result = await executeAt(i, token, true, app.script.commands.length - 1);
    if (result?.jumpIndex != null && result.jumpIndex >= 0 && result.jumpIndex < app.script.commands.length) {
      app.current = result.jumpIndex;
    }
  }
  return {
    storyId: app.story.id,
    commands: app.script.commands.length,
    current: app.current,
    registry: commandRegistrySnapshot(),
  };
}

async function executeCommandForTest(commandName, args = [], replaying = true) {
  const token = ++app.runToken;
  const command = { index: 0, command: commandName, rawCommand: commandName, args };
  return app.registry.execute(new NovelContext(app, app.controller, token, replaying), command);
}

function commandRegistrySnapshot() {
  return Array.from(app.registry.commands.keys()).sort();
}

function exposeManualReader() {
  window.manualReader = {
    app,
    loadStory,
    nextText,
    stepCommand,
    replayTo,
    replayStoryForTest,
    executeCommandForTest,
    commandRegistrySnapshot,
  };
}

function updatePosition() {
  el.seek.value = Math.max(0, app.current);
  el.position.textContent = `${Math.max(0, app.current + 1)} / ${app.script?.commands.length || 0}`;
  document.querySelectorAll('.command-item').forEach((item) => {
    item.classList.toggle('active', Number(item.dataset.index) === app.current);
  });
  const active = el.commandList.querySelector('.command-item.active');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

function updateInspectors() {
  el.modelInfo.textContent = JSON.stringify({
    live2dReady: app.controller.live2d.ready,
    live2dVisible: app.controller.live2d.visible,
    motion: app.controller.live2d.currentMotion,
    mosaic: {
      mode: app.controller.live2d.mosaicMode,
      status: app.controller.live2d.mosaicStatus,
      drawables: app.controller.live2d.mosaicDrawables?.map((item) => `${item.id}:${item.material}`) || [],
    },
    audio: app.controller.sound.currentLabels(),
  }, null, 2);
  el.stateInfo.textContent = JSON.stringify({
    current: app.current,
    message: app.controller.message.state,
    scene: app.controller.scene.state,
    pendingAsync: app.controller.async.pendingLabels(),
  }, null, 2);
}

function toggleAuto() {
  app.auto = !app.auto;
  el.autoBtn.classList.toggle('active', app.auto);
  queueAuto();
}

function handleFastForwardKeyDown(event) {
  if (event.key !== 'Control' || event.repeat || app.fastForward) return;
  const target = event.target;
  if (target instanceof Element && target.closest('input, select, textarea, [contenteditable="true"]')) return;
  if (document.querySelector('.reader-shell')?.dataset.view !== 'player') return;
  startFastForward();
}

function handleFastForwardKeyUp(event) {
  if (event.key === 'Control') stopFastForward();
}

function startFastForward() {
  if (app.fastForward || !app.script) return;
  app.fastForward = true;
  app.fastForwardGeneration += 1;
  if (app.autoTimer) clearTimeout(app.autoTimer);
  app.autoTimer = null;
  document.querySelector('.reader-shell')?.classList.add('fast-forwarding');
  runFastForward(app.fastForwardGeneration);
}

function stopFastForward(resumeAuto = true) {
  if (!app.fastForward) return;
  app.fastForward = false;
  app.fastForwardGeneration += 1;
  document.querySelector('.reader-shell')?.classList.remove('fast-forwarding');
  if (resumeAuto && app.auto) queueAuto();
}

async function runFastForward(generation) {
  while (app.fastForward && generation === app.fastForwardGeneration) {
    if (!app.running) {
      if (!app.script || app.current + 1 >= app.script.commands.length) {
        stopFastForward();
        return;
      }
      await nextText();
    }
    await new Promise((resolve) => setTimeout(resolve, 35));
  }
}

function stopAuto() {
  app.auto = false;
  el.autoBtn.classList.remove('active');
  if (app.autoTimer) clearTimeout(app.autoTimer);
  app.autoTimer = null;
}

function completeCurrentMessagePause() {
  const command = app.script?.commands?.[app.current];
  if (!command || !isTextPauseCommand(command.command)) return;
  // 保留上一句语音，直到下一条语音播放时由同一声道替换。
  app.controller.live2d.stopLipSync();
}

function queueAuto() {
  if (app.autoTimer) clearTimeout(app.autoTimer);
  if (!app.auto || app.running || app.fastForward) return;
  const state = app.controller.message.state;
  const text = state.text || '';
  const textMs = clamp(1600 + text.length * 55, 2200, 12000);
  const shownAt = state.shownAt || performance.now();
  const textRemainingMs = Math.max(0, textMs - (performance.now() - shownAt));
  const voiceCue = state.voiceCue || '';
  const voiceDurationMs = voiceCue ? Math.max(0, app.controller.sound.cueDuration(voiceCue) * 1000) : 0;
  const voiceEstimatedRemainingMs = voiceDurationMs > 0
    ? Math.max(0, voiceDurationMs - (performance.now() - shownAt))
    : 0;
  if (voiceCue && app.controller.sound.isVoicePendingOrPlaying()) {
    const playingRemainingMs = app.controller.sound.remainingVoiceMs();
    const remainingMs = Math.max(textRemainingMs, voiceEstimatedRemainingMs, playingRemainingMs);
    app.autoTimer = setTimeout(queueAuto, Math.max(AUTO_CHECK_MS, Math.min(remainingMs || AUTO_CHECK_MS, 500)));
    return;
  }
  const endedAt = voiceCue ? app.controller.sound.voiceEndedAt(voiceCue) : 0;
  const afterVoiceRemainingMs = endedAt ? Math.max(0, AUTO_AFTER_VOICE_MS - (performance.now() - endedAt)) : 0;
  const waitMs = Math.max(textRemainingMs, voiceEstimatedRemainingMs, afterVoiceRemainingMs);
  app.autoTimer = setTimeout(() => {
    if (!app.auto || app.running) {
      queueAuto();
      return;
    }
    if (voiceCue && app.controller.sound.isVoicePendingOrPlaying()) {
      queueAuto();
      return;
    }
    nextText();
  }, waitMs > 0 ? Math.max(AUTO_CHECK_MS, waitMs) : 0);
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach((panel) => panel.classList.toggle('active', panel.id === `${name}Tab`));
}

class NovelContext {
  constructor(player, models, token, replaying, replayTargetIndex = -1) {
    this.player = player;
    this.models = models;
    this.token = token;
    this.asyncToken = player.asyncToken;
    this.replaying = replaying;
    this.replayTargetIndex = replayTargetIndex;
  }

  isReplayTarget(command) {
    return this.replaying && Number(command?.index) === Number(this.replayTargetIndex);
  }
}

class NovelArguments {
  constructor(command) {
    this.command = command;
    this.args = command.args || [];
  }

  string(index, fallback = '') {
    const value = this.args[index - 1];
    return value == null || value === '' ? fallback : String(value);
  }

  float(index, fallback = 0) {
    const value = Number.parseFloat(this.args[index - 1]);
    return Number.isFinite(value) ? value : fallback;
  }

  int(index, fallback = 0) {
    const value = Number.parseInt(this.args[index - 1], 10);
    return Number.isFinite(value) ? value : fallback;
  }

  on(index, fallback = true) {
    return parseOnOff(this.args[index - 1], fallback);
  }
}

class NovelModelController {
  constructor() {
    this.message = new NovelModelMessage();
    this.sound = new NovelModelSound();
    this.live2d = new NovelModelLive2D();
    this.screen = new NovelModelScreen();
    this.scene = new NovelModelScene();
    this.async = new NovelModelAsync();
  }

  reset(story, basePath) {
    this.message.reset();
    this.sound.stopAll();
    this.live2d.reset();
    this.screen.reset();
    this.scene.reset();
    this.async.reset();
    this.story = story;
    this.basePath = basePath;
  }
}

class NovelModelMessage {
  constructor() {
    this.reset();
  }

  reset() {
    this.state = { speaker: '', text: '', voiceCue: '', visible: true, shownAt: performance.now() };
    el.speaker.textContent = '';
    el.message.textContent = '';
    el.messageBox.classList.remove('thought-message');
    el.messageBox.style.opacity = '1';
  }

  show(speaker, text, voiceCue = '') {
    const resolvedSpeaker = replacePlayerName(speaker);
    const resolvedText = replacePlayerName(text);
    this.state = { speaker: resolvedSpeaker, text: resolvedText, voiceCue, visible: true, shownAt: performance.now() };
    el.speaker.textContent = resolvedSpeaker || '';
    el.message.innerHTML = escapeHtml(resolvedText || '').replace(/&lt;br&gt;/g, '<br>');
    el.messageBox.classList.toggle('thought-message', isThoughtMessage(resolvedText));
    el.messageBox.style.opacity = '1';
  }

  setWindow(visible, seconds) {
    this.state.visible = visible;
    el.messageBox.style.transitionDuration = `${Math.max(0, seconds) * 1000}ms`;
    el.messageBox.style.opacity = visible ? '1' : '0';
  }
}

class NovelModelScreen {
  constructor() {
    this.crossFadeSnapshot = null;
    this.crossFadeTimer = 0;
    this.transitionSnapshot = null;
    this.transitionTimer = 0;
  }

  reset() {
    this.state = { fade: null, blur: null, linework: null, colorFade: null, screenEffects: {} };
    el.fadeLayer.style.opacity = '0';
    el.fadeLayer.style.transitionDuration = '0ms';
    el.fadeLayer.style.background = '#000';
    el.screenEffectLayer.style.opacity = '0';
    el.screenEffectLayer.style.transitionDuration = '0ms';
    el.screenEffectLayer.style.background = 'rgba(0,0,0,0)';
    el.screenEffectLayer.style.filter = '';
    el.screenEffectLayer.dataset.effects = '';
    el.stage.classList.remove('background-blur-active');
    this.clearCrossFade();
    this.clearTransitionCrossFade();
    for (const layer of [el.bgLayer, el.live2dCanvas, el.mosaicLayer, el.fallbackTexture, el.sceneLayer]) {
      layer.style.transition = '';
      layer.style.filter = '';
    }
  }

  fade(direction, color, seconds) {
    const out = String(direction || '').toLowerCase() === 'out';
    this.state.fade = { direction, color, seconds };
    el.fadeLayer.style.background = cssColor(color || 'black');
    el.fadeLayer.style.transitionDuration = `${Math.max(0, seconds) * 1000}ms`;
    requestAnimationFrame(() => {
      el.fadeLayer.style.opacity = out ? '1' : '0';
    });
  }

  captureCrossFade() {
    this.crossFadeSnapshot = buildTransitionSnapshot();
    this.state.crossFadeCapture = { capturedAt: performance.now() };
  }

  crossFade(seconds, endX = 0, endY = 0, endScaleX = 1, endScaleY = 1) {
    const duration = Math.max(0, Number(seconds) || 0);
    const snapshot = this.crossFadeSnapshot || buildTransitionSnapshot();
    const rect = el.stage.getBoundingClientRect();
    const unitScale = Math.max(0.01, (rect.height || 1) / 2048);
    const tx = (Number(endX) || 0) * unitScale;
    const ty = -(Number(endY) || 0) * unitScale;
    const sx = Number.isFinite(Number(endScaleX)) ? Number(endScaleX) : 1;
    const sy = Number.isFinite(Number(endScaleY)) ? Number(endScaleY) : 1;
    this.state.crossFade = { seconds: duration, endX, endY, endScaleX: sx, endScaleY: sy };
    if (this.crossFadeTimer) clearTimeout(this.crossFadeTimer);
    if (this.transitionTimer) {
      clearTimeout(this.transitionTimer);
      this.transitionTimer = 0;
    }
    this.state.transition = null;
    el.transitionLayer.replaceChildren(snapshot);
    el.transitionLayer.dataset.effect = 'crossfade';
    el.transitionLayer.style.display = 'block';
    el.transitionLayer.style.opacity = '1';
    el.transitionLayer.style.transformOrigin = '50% 50%';
    el.transitionLayer.style.transform = 'translate(0px, 0px) scale(1, 1)';
    el.transitionLayer.style.transitionProperty = 'opacity, transform';
    el.transitionLayer.style.transitionTimingFunction = 'linear';
    el.transitionLayer.style.transitionDuration = '0ms';

    if (duration <= 0) {
      el.transitionLayer.style.transform = `translate(${tx}px, ${ty}px) scale(${sx}, ${sy})`;
      this.clearCrossFade();
      return;
    }

    requestAnimationFrame(() => {
      el.transitionLayer.style.transitionDuration = `${duration * 1000}ms`;
      el.transitionLayer.style.opacity = '0';
      el.transitionLayer.style.transform = `translate(${tx}px, ${ty}px) scale(${sx}, ${sy})`;
    });
    this.crossFadeTimer = setTimeout(() => this.clearCrossFade(), (duration * 1000) + 80);
  }

  clearCrossFade() {
    this.state.crossFade = null;
    if (this.crossFadeTimer) {
      clearTimeout(this.crossFadeTimer);
      this.crossFadeTimer = 0;
    }
    if (el.transitionLayer.dataset.effect === 'crossfade') {
      el.transitionLayer.replaceChildren();
      el.transitionLayer.style.display = 'none';
      el.transitionLayer.style.opacity = '0';
      el.transitionLayer.style.transform = '';
      el.transitionLayer.style.transformOrigin = '';
      el.transitionLayer.style.transitionDuration = '0ms';
      el.transitionLayer.style.transitionProperty = 'opacity';
      el.transitionLayer.dataset.effect = '';
    }
  }

  colorFade(seconds, r, g, b, fromAlpha, toAlpha) {
    const duration = Math.max(0, Number(seconds) || 0);
    const color = rgbaByte(r, g, b, 1);
    this.state.colorFade = { seconds: duration, r, g, b, fromAlpha, toAlpha };
    el.screenEffectLayer.style.background = color;
    el.screenEffectLayer.style.filter = el.screenEffectLayer.style.filter || '';
    el.screenEffectLayer.style.transitionDuration = '0ms';
    el.screenEffectLayer.style.opacity = String(byteToUnit(fromAlpha));
    requestAnimationFrame(() => {
      el.screenEffectLayer.style.transitionDuration = `${duration * 1000}ms`;
      el.screenEffectLayer.style.opacity = String(byteToUnit(toAlpha));
    });
  }

  screenEffect(effect, on, seconds, value) {
    const key = normalizeAssetKey(effect);
    if (!key) return;
    this.state.screenEffects[key] = { effect, on, seconds, value };
    const duration = Math.max(0, Number(seconds) || 0);
    el.screenEffectLayer.style.transitionDuration = `${duration * 1000}ms`;
    const active = Object.values(this.state.screenEffects).filter((item) => item.on);
    el.screenEffectLayer.dataset.effects = active.map((item) => item.effect).join(',');
    const filters = active.map((item) => screenEffectFilter(item.effect, item.value)).filter(Boolean);
    el.screenEffectLayer.style.filter = filters.join(' ');
    if (!active.length && !this.state.colorFade) el.screenEffectLayer.style.opacity = '0';
  }

  captureTransitionCrossFade() {
    this.transitionSnapshot = buildTransitionSnapshot();
    this.state.transitionCapture = { capturedAt: performance.now() };
  }

  transitionCrossFade(transitionName, seconds) {
    const duration = Math.max(0, Number(seconds) || 0);
    const snapshot = this.transitionSnapshot || buildTransitionSnapshot();
    this.state.transition = { name: transitionName || '', seconds: duration };
    if (this.transitionTimer) clearTimeout(this.transitionTimer);
    if (this.crossFadeTimer) {
      clearTimeout(this.crossFadeTimer);
      this.crossFadeTimer = 0;
    }
    this.state.crossFade = null;
    el.transitionLayer.replaceChildren(snapshot);
    el.transitionLayer.dataset.effect = 'transition-crossfade';
    el.transitionLayer.style.display = 'block';
    el.transitionLayer.style.opacity = '1';
    el.transitionLayer.style.transform = '';
    el.transitionLayer.style.transformOrigin = '';
    el.transitionLayer.style.transitionProperty = 'opacity';
    el.transitionLayer.style.transitionTimingFunction = 'linear';
    el.transitionLayer.style.transitionDuration = '0ms';

    if (duration <= 0) {
      this.clearTransitionCrossFade();
      return;
    }

    requestAnimationFrame(() => {
      el.transitionLayer.style.transitionDuration = `${duration * 1000}ms`;
      el.transitionLayer.style.opacity = '0';
    });
    this.transitionTimer = setTimeout(() => this.clearTransitionCrossFade(), (duration * 1000) + 80);
  }

  transitionFade(ruleName, direction, colorName, seconds) {
    const duration = Math.max(0, Number(seconds) || 0);
    const fadeIn = normalizeAssetKey(direction) === 'in';
    const white = normalizeAssetKey(colorName) === 'white';
    const color = white ? '#fff' : '#000';
    this.state.transitionFade = { ruleName: ruleName || '', direction, colorName, seconds: duration };
    el.fadeLayer.style.background = color;
    el.fadeLayer.style.transitionDuration = '0ms';
    if (fadeIn) el.fadeLayer.style.opacity = '1';
    requestAnimationFrame(() => {
      el.fadeLayer.style.transitionDuration = `${duration * 1000}ms`;
      el.fadeLayer.style.opacity = fadeIn ? '0' : '1';
    });
    if (fadeIn && duration > 0) {
      window.setTimeout(() => {
        if (this.state.transitionFade?.ruleName === (ruleName || '') && normalizeAssetKey(this.state.transitionFade.direction) === 'in') {
          el.fadeLayer.style.transitionDuration = '0ms';
          el.fadeLayer.style.opacity = '0';
        }
      }, (duration * 1000) + 80);
    }
  }

  clearTransitionCrossFade() {
    if (this.transitionTimer) clearTimeout(this.transitionTimer);
    this.transitionTimer = 0;
    this.state.transition = null;
    this.transitionSnapshot = null;
    if (el.transitionLayer.dataset.effect !== 'transition-crossfade') return;
    el.transitionLayer.replaceChildren();
    el.transitionLayer.style.display = 'none';
    el.transitionLayer.style.opacity = '0';
    el.transitionLayer.style.transform = '';
    el.transitionLayer.style.transformOrigin = '';
    el.transitionLayer.style.transitionProperty = 'opacity';
    el.transitionLayer.style.transitionDuration = '0ms';
    el.transitionLayer.dataset.effect = '';
  }

  clearSkipArtifacts() {
    this.state.linework = null;
    this.clearCrossFade();
    this.clearTransitionCrossFade();
  }

  blur(on, target, seconds) {
    const normalizedTarget = normalizeAssetKey(target || 'SCREEN');
    const layers = screenBlurTargetLayers(normalizedTarget);
    this.state.blur = { on, target: target || '', seconds };
    for (const layer of layers) {
      layer.style.transition = `filter ${Math.max(0, seconds) * 1000}ms linear`;
      layer.style.filter = on ? 'blur(5px)' : '';
    }
    el.stage.classList.toggle('background-blur-active', on && layers.includes(el.bgLayer));
    app.models?.scene?.applyLayerTransforms();
  }
}

class NovelModelScene {
  constructor() {
    this.assetCache = new Map();
    this.reset();
  }

  reset() {
    this.state = {
      characters: {},
      objects: {},
      prefabs: {},
      backgrounds: {},
      subimages: {},
      stills: {},
      background: null,
      backgroundTransform: { x: 0, y: 0, scaleX: 1, scaleY: 1, seconds: 0, easing: 'linear' },
      backgroundColor: { r: 0, g: 0, b: 0, alpha: 0, seconds: 0 },
      camera: { x: 0, y: 0, zoom: 1, seconds: 0, easing: 'linear' },
    };
    el.sceneLayer.replaceChildren();
    this.clearBackground();
    this.applyLayerTransforms();
  }

  target(bucketName, tag) {
    const bucket = this.state[bucketName];
    const key = tag || 'default';
    bucket[key] = bucket[key] || {
      tag: key,
      visible: false,
      deleted: false,
      alpha: 0,
      x: 0,
      y: 0,
      charaHeight: 0,
      scale: 1,
      transitionSeconds: 0,
      easing: 'linear',
      rotation: 0,
      priority: 0,
      layer: '',
      frontBack: '',
    };
    return bucket[key];
  }

  async loadCharacter(tag, characterId, displayName) {
    const target = this.target('characters', tag);
    const id = String(characterId || '').toUpperCase();
    target.kind = 'character';
    target.characterId = id;
    target.displayName = displayName || '';
    target.assetBase = `${CHARA_DATA_ROOT}${id.toLowerCase()}/`;
    target.asset = await this.loadCharacterAsset(id);
    target.loaded = !!target.asset?.files?.body;
    target.deleted = false;
    target.visible = target.visible === true;
    target.alpha = Number.isFinite(target.alpha) ? target.alpha : 0;
    target.baseFace = target.baseFace || 'normal';
    target.eyeClosed = !!target.eyeClosed;
    target.transitionSeconds = 0;
    return target;
  }

  async loadCharacterAsset(characterId) {
    const key = String(characterId || '').toLowerCase();
    if (!key) return null;
    if (!this.assetCache.has(key)) {
      this.assetCache.set(key, fetchJson(`${CHARA_DATA_ROOT}${key}/meta.json`).catch((error) => {
        setStatus(`chara asset missing: ${characterId} (${error.message})`, true);
        return null;
      }));
    }
    return this.assetCache.get(key);
  }

  faceCharacter(tag, faceName) {
    const target = this.target('characters', tag);
    const normalized = normalizeCharaFace(faceName);
    target.lastFaceCommand = faceName;
    target.transitionSeconds = 0;
    if (normalized === 'closed') {
      target.eyeClosed = true;
      return;
    }
    if (normalized === 'eyeopen') {
      target.eyeClosed = false;
      return;
    }
    target.baseFace = normalized || 'normal';
  }

  poseCharacter(tag, pose) {
    const target = this.target('characters', tag);
    target.pose = Number(pose) || 0;
  }

  moveCharacter(tag, moveType, parameter, value, seconds) {
    const target = this.target('characters', tag);
    const resolved = resolveCharaMoveParameter(parameter, value, target);
    const move = resolveCharaMoveValue(moveType, resolved.value, resolved.axis === 'x' ? target.x : target.y);
    if (resolved.axis === 'x') target.x = move.value;
    if (resolved.axis === 'y') target.y = move.value;
    target.moveType = String(moveType || 'Set');
    target.moveParameter = String(parameter || '');
    target.transitionSeconds = move.instant ? 0 : Math.max(0, Number(seconds) || 0);
    target.easing = charaMoveEasing(moveType);
  }

  scaleCharacter(tag, scale, seconds) {
    const target = this.target('characters', tag);
    target.scale = clamp(Number(scale) || 1, 0.05, 5);
    target.transitionSeconds = Math.max(0, Number(seconds) || 0);
    target.easing = 'ease-out';
  }

  showCharacter(tag, seconds, alpha = 1) {
    const target = this.target('characters', tag);
    const value = Number(alpha);
    target.focus = true;
    target.visible = true;
    target.deleted = false;
    target.alpha = clamp(Number.isFinite(value) ? value : 1, 0, 1);
    target.transitionSeconds = Math.max(0, Number(seconds) || 0);
    target.easing = 'linear';
  }

  hideCharacter(tag, seconds) {
    const target = this.target('characters', tag);
    target.visible = false;
    target.alpha = 0;
    target.transitionSeconds = Math.max(0, Number(seconds) || 0);
    target.easing = 'linear';
  }

  deleteCharacter(tag) {
    const target = this.target('characters', tag);
    target.deleted = true;
    target.visible = false;
    target.alpha = 0;
  }

  emotionCharacter(tag, emotionName) {
    const target = this.target('characters', tag);
    target.emotion = normalizeAssetKey(emotionName);
    target.emotionRaw = emotionName || '';
    target.emotionNonce = (target.emotionNonce || 0) + 1;
    target.emotionStartedAt = performance.now();
    target.emotionDurationSeconds = emotionDurationSeconds(target.emotion);
    return target.emotionDurationSeconds;
  }

  clearEmotion(tag) {
    const target = this.target('characters', tag);
    target.emotion = '';
    target.emotionRaw = '';
    target.emotionDurationSeconds = 0;
  }

  focusAllCharacters() {
    this.setAllCharacterFocus(true);
  }

  setAllCharacterFocus(focused) {
    for (const item of Object.values(this.state.characters)) item.focus = !!focused;
  }

  focusCharacter(tag, focused) {
    const target = this.target('characters', tag);
    target.focus = !!focused;
  }

  maskCharacter(tag, start, end, seconds) {
    const target = this.target('characters', tag);
    target.mask = { start: Number(start) || 0, end: Number(end) || 0, seconds: Math.max(0, Number(seconds) || 0) };
    target.transitionSeconds = target.mask.seconds;
  }

  itemCharacter(tag, itemNo, alpha, beforeAlpha, seconds) {
    const target = this.target('characters', tag);
    target.item = {
      itemNo: Number(itemNo) || 0,
      alpha: Number(alpha) || 0,
      beforeAlpha: Number(beforeAlpha) || 0,
      seconds: Math.max(0, Number(seconds) || 0),
    };
    target.transitionSeconds = target.item.seconds;
  }

  setDefaultCharacterColor() {
    for (const item of Object.values(this.state.characters)) {
      item.color = null;
      item.focus = true;
    }
  }

  reactCharacter(tag, reactionName, seconds) {
    const target = this.target('characters', tag);
    target.reaction = normalizeAssetKey(reactionName);
    target.reactionSeconds = Math.max(0, Number(seconds) || 0);
    target.reactionNonce = (target.reactionNonce || 0) + 1;
  }

  setBackgroundColor(r, g, b, alpha, seconds) {
    this.state.backgroundColor = { r, g, b, alpha, seconds: Math.max(0, Number(seconds) || 0) };
    el.bgLayer.style.transition = `background-color ${this.state.backgroundColor.seconds * 1000}ms linear`;
    el.bgLayer.style.backgroundColor = rgbaByte(r, g, b, alpha);
  }

  setBackground(backgroundName, applyCharaColor = true) {
    const key = normalizeAssetKey(backgroundName);
    if (!key) return;
    this.state.background = { name: key, rawName: backgroundName, applyCharaColor };
    el.stage.dataset.background = key;
    el.bgLayer.dataset.background = key;
    el.bgLayer.style.opacity = '1';
    el.bgLayer.style.backgroundImage = `url("${BG_DATA_ROOT}${key}.png")`;
    this.verifyBackgroundImage(key);
  }

  clearBackground() {
    el.stage.dataset.background = '';
    el.bgLayer.dataset.background = '';
    el.bgLayer.style.opacity = '0';
    el.bgLayer.style.backgroundImage = '';
    el.bgLayer.style.filter = '';
    el.bgLayer.style.backgroundColor = 'rgba(0,0,0,0)';
  }

  moveCamera(easing, axis, value, seconds) {
    const moveType = normalizeCharaMoveType(easing);
    const key = normalizeAssetKey(axis);
    const nextValue = Number(value) || 0;
    this.state.camera.seconds = Math.max(0, Number(seconds) || 0);
    this.state.camera.easing = easingToCss(easing);
    const applyAxis = (axisName) => {
      const current = Number(this.state.camera[axisName]) || 0;
      this.state.camera[axisName] = moveType === 'add' ? current + nextValue : nextValue;
    };
    if (['x', 'horizontal', 'left', 'right'].includes(key)) applyAxis('x');
    else if (['y', 'vertical', 'up', 'down', 'top', 'bottom'].includes(key)) applyAxis('y');
    else {
      applyAxis('x');
      applyAxis('y');
    }
    this.applyLayerTransforms();
  }

  zoomCamera(zoom, seconds, easing) {
    const moveType = normalizeCharaMoveType(easing);
    const value = Number(zoom) || 1;
    const current = Number(this.state.camera.zoom) || 1;
    this.state.camera.zoom = clamp(moveType === 'add' ? current + value : value, 0.05, 8);
    this.state.camera.seconds = Math.max(0, Number(seconds) || 0);
    this.state.camera.easing = easingToCss(easing);
    this.applyLayerTransforms();
  }

  moveTarget(targetType, tag, seconds, x, y, easing) {
    const type = normalizeTargetType(targetType);
    const duration = Math.max(0, Number(seconds) || 0);
    const tx = Number(x) || 0;
    const ty = Number(y) || 0;
    const cssEasing = easingToCss(easing);
    if (type === 'back') {
      this.state.backgroundTransform.x = -tx;
      this.state.backgroundTransform.y = ty;
      this.state.backgroundTransform.seconds = duration;
      this.state.backgroundTransform.easing = cssEasing;
      this.applyLayerTransforms();
      return;
    }
    const item = this.sceneTargetForType(type, tag);
    item.x = tx;
    item.y = ty;
    item.transitionSeconds = duration;
    item.easing = cssEasing;
    item.visible = true;
  }

  scaleTarget(targetType, tag, seconds, scaleX, scaleY, easing) {
    const type = normalizeTargetType(targetType);
    const duration = Math.max(0, Number(seconds) || 0);
    const sx = Number.isFinite(Number(scaleX)) ? Number(scaleX) : 1;
    const sy = Number.isFinite(Number(scaleY)) ? Number(scaleY) : sx;
    const cssEasing = easingToCss(easing);
    if (type === 'back') {
      this.state.backgroundTransform.scaleX = sx;
      this.state.backgroundTransform.scaleY = sy;
      this.state.backgroundTransform.seconds = duration;
      this.state.backgroundTransform.easing = cssEasing;
      this.applyLayerTransforms();
      return;
    }
    const item = this.sceneTargetForType(type, tag);
    item.scaleX = sx;
    item.scaleY = sy;
    item.scale = sx;
    item.transitionSeconds = duration;
    item.easing = cssEasing;
    item.visible = true;
  }

  colorCharacter(tag, seconds, r, g, b, alpha) {
    const target = this.target('characters', tag);
    target.color = { r, g, b, alpha };
    target.transitionSeconds = Math.max(0, Number(seconds) || 0);
  }

  createSubImage(asset, tag, alpha, layer, layerId, layerPosTag, layerTargetObject) {
    const target = this.target('subimages', tag || asset || 'subimage');
    const assetText = String(asset || '');
    const imageInfo = resolveSceneAssetInfo(assetText);
    target.kind = 'subimage';
    target.asset = assetText;
    target.visible = true;
    target.deleted = false;
    target.x = 0;
    target.y = 0;
    target.scale = 1;
    target.scaleX = 1;
    target.scaleY = 1;
    target.layer = layer || '';
    target.layerId = Number(layerId) || 0;
    target.layerPosTag = layerPosTag || '';
    target.layerTargetObject = layerTargetObject || '';
    target.priority = Number(layerId) || target.priority || 0;
    target.alpha = normalizeAlpha(alpha);
    target.imageSrc = imageInfo.src;
    target.assetWidth = imageInfo.width;
    target.assetHeight = imageInfo.height;
    target.fullFrame = imageInfo.fullFrame;
  }

  fadeSubImage(tag, fromAlpha, toAlpha, seconds) {
    const target = this.target('subimages', tag);
    target.visible = true;
    target.deleted = false;
    target.alpha = normalizeAlpha(fromAlpha);
    target.transitionSeconds = 0;
    this.render();
    target.transitionSeconds = Math.max(0, Number(seconds) || 0);
    const finalAlpha = normalizeAlpha(toAlpha);
    target.alpha = finalAlpha;
    if (finalAlpha <= 0) {
      window.setTimeout(() => {
        if (target.alpha <= 0 && !target.deleted) {
          target.visible = false;
          this.render();
        }
      }, target.transitionSeconds * 1000);
    }
  }

  deleteSubImage(tag) {
    const target = this.target('subimages', tag);
    target.deleted = true;
    target.visible = false;
    target.alpha = 0;
  }

  loadStill(tag, asset) {
    const target = this.target('stills', tag || asset || 'still');
    const assetText = String(asset || '');
    const imageInfo = resolveSceneAssetInfo(assetText);
    target.kind = 'still';
    target.asset = assetText;
    target.deleted = false;
    target.visible = false;
    target.alpha = 0;
    target.x = 0;
    target.y = 0;
    target.scale = 1;
    target.priority = 30;
    target.stillIndex = 1;
    target.imageSrc = imageInfo.src;
    target.assetWidth = imageInfo.width;
    target.assetHeight = imageInfo.height;
    target.fullFrame = true;
  }

  changeStill(tag, index) {
    const target = this.target('stills', tag);
    target.kind = 'still';
    target.stillIndex = Number.isFinite(Number(index)) ? Math.trunc(Number(index)) : 1;
    if (!target.asset) target.asset = tag || 'still';
    if (!target.imageSrc) {
      const imageInfo = resolveSceneAssetInfo(target.asset);
      target.imageSrc = imageInfo.src;
      target.assetWidth = imageInfo.width;
      target.assetHeight = imageInfo.height;
      target.fullFrame = true;
    }
  }

  fadeStill(tag, visible, seconds) {
    const target = this.target('stills', tag);
    target.kind = 'still';
    target.deleted = false;
    target.visible = true;
    target.transitionSeconds = Math.max(0, Number(seconds) || 0);
    target.alpha = visible ? 1 : 0;
    if (!visible) {
      window.setTimeout(() => {
        if (target.alpha === 0) {
          target.visible = false;
          this.render();
        }
      }, target.transitionSeconds * 1000);
    }
  }

  deleteStill(tag) {
    const target = this.target('stills', tag);
    target.deleted = true;
    target.visible = false;
    target.alpha = 0;
  }

  loadPrefabUi(tag, asset, targetType, x, layer, option) {
    const target = this.target('prefabs', tag || asset || 'prefab');
    target.kind = 'prefab';
    target.asset = asset || '';
    target.targetType = targetType || '';
    target.x = Number(x) || 0;
    target.y = 0;
    target.layer = layer || '';
    target.option = option || '';
    target.visible = false;
    target.alpha = 0;
  }

  loadPrefab(tag, asset, targetType, x, layer, option) {
    const target = this.target('prefabs', tag || asset || 'prefab');
    target.kind = 'prefab';
    target.asset = asset || '';
    target.targetType = targetType || '';
    target.x = Number(x) || 0;
    target.y = 0;
    target.layer = layer || '';
    target.option = option || '';
    target.visible = false;
    target.alpha = 0;
  }

  showPrefab(tag, on, seconds, x, y, option) {
    const target = this.target('prefabs', tag || 'prefab');
    target.kind = 'prefab';
    target.visible = !!on;
    target.alpha = on ? 1 : 0;
    target.x = Number.isFinite(Number(x)) ? Number(x) : target.x || 0;
    target.y = Number.isFinite(Number(y)) ? Number(y) : target.y || 0;
    target.option = option || target.option || '';
    target.transitionSeconds = Math.max(0, Number(seconds) || 0);
  }

  showPrefabUi(tag, on, seconds, x, y) {
    const target = this.target('prefabs', tag || 'prefab');
    target.visible = !!on;
    target.alpha = on ? 1 : 0;
    target.x = Number.isFinite(Number(x)) ? Number(x) : target.x || 0;
    target.y = Number.isFinite(Number(y)) ? Number(y) : target.y || 0;
    target.transitionSeconds = Math.max(0, Number(seconds) || 0);
  }

  showPrefabAll(tag, asset, on, seconds, x, option) {
    const target = this.target('prefabs', tag || asset || 'prefaball');
    target.kind = 'prefaball';
    target.asset = asset || target.asset || '';
    target.visible = !!on;
    target.alpha = on ? 1 : 0;
    target.x = Number.isFinite(Number(x)) ? Number(x) : target.x || 0;
    target.option = option || target.option || '';
    target.transitionSeconds = Math.max(0, Number(seconds) || 0);
  }

  deletePrefabUi(tag) {
    const target = this.target('prefabs', tag);
    target.deleted = true;
    target.visible = false;
    target.alpha = 0;
  }

  deletePrefab(tag) {
    const target = this.target('prefabs', tag);
    target.deleted = true;
    target.visible = false;
    target.alpha = 0;
  }

  deletePrefabAll() {
    for (const target of Object.values(this.state.prefabs)) {
      target.deleted = true;
      target.visible = false;
      target.alpha = 0;
    }
  }

  setPriority(tag, targetType, priority, frontBack, group) {
    const type = normalizeTargetType(targetType);
    const item = this.sceneTargetForType(type, tag);
    item.priority = Number(priority) || 0;
    item.frontBack = frontBack || '';
    item.group = group || '';
  }

  showItem(on, itemId) {
    const target = this.target('objects', `item:${itemId || 'item'}`);
    target.kind = 'item';
    target.asset = String(itemId || '');
    target.visible = !!on;
    target.alpha = on ? 1 : 0;
  }

  shakeAll(x, y, z, count, seconds, random, value, loop) {
    this.applyShakeToElement(el.stage, 'shakeall', { x, y, z, count, seconds, random, value, loop });
  }

  stopShakeAll() {
    el.stage.getAnimations().forEach((animation) => {
      if (animation.effect?.target === el.stage) animation.cancel();
    });
  }

  shakeTarget(targetType, tag, x, y, seconds, count, random, loop, value) {
    const type = normalizeTargetType(targetType);
    if (type === 'back') {
      this.applyShakeToElement(el.bgLayer, `shake:${type}`, { x, y, count, seconds, random, loop, value });
      return;
    }
    const item = this.sceneTargetForType(type, tag);
    item.shake = { x, y, seconds, count, random, loop, value, nonce: (item.shake?.nonce || 0) + 1 };
  }

  jumpTarget(targetType, tag, power, seconds, count, loop) {
    const item = this.sceneTargetForType(normalizeTargetType(targetType), tag);
    item.jump = {
      power: Number(power) || 0,
      seconds: Math.max(0.001, Number(seconds) || 1),
      count: Math.max(1, Number(count) || 1),
      loop: Boolean(loop),
      nonce: (item.jump?.nonce || 0) + 1,
    };
  }

  stopJumpTarget(targetType, tag) {
    const item = this.sceneTargetForType(normalizeTargetType(targetType), tag);
    item.jump = { ...(item.jump || {}), stopNonce: (item.jump?.stopNonce || 0) + 1 };
    const key = item.loaded ? `character:${item.tag}` : `token:${item.tag}`;
    const node = el.sceneLayer.querySelector(`[data-scene-key="${cssEscape(key)}"]`);
    if (node) this.cancelAnimationById(node, `jump:${item.tag}`);
  }

  applyShakeToElement(node, id, options) {
    const duration = Math.max(0.001, Number(options.seconds) || 1) * 1000;
    const requestedIterations = Math.max(1, Number(options.count) || 1);
    const maxIterations = Math.max(1, Math.ceil(SCENE_SHAKE_MAX_MS / duration));
    const iterations = options.loop ? maxIterations : Math.min(requestedIterations, maxIterations);
    const x = Number(options.x) || 0;
    const y = Number(options.y) || 0;
    this.cancelAnimationById(node, id);
    const animation = node.animate([
      { transform: node.style.transform || 'translate(0, 0)' },
      { transform: `${node.style.transform || ''} translate(${x}px, ${-y}px)` },
      { transform: `${node.style.transform || ''} translate(${-x}px, ${y}px)` },
      { transform: node.style.transform || 'translate(0, 0)' },
    ], { duration, iterations, easing: 'linear' });
    animation.id = id;
    window.setTimeout(() => {
      try { animation.cancel(); } catch (_) {}
    }, Math.min(SCENE_SHAKE_MAX_MS, duration * iterations) + 80);
  }

  applyJumpToElement(node, item, unitScale) {
    if (item.jump?.stopNonce && node.dataset.jumpStopNonce !== String(item.jump.stopNonce)) {
      node.dataset.jumpStopNonce = String(item.jump.stopNonce);
      this.cancelAnimationById(node, `jump:${item.tag}`);
    }
    if (!item.jump?.nonce || node.dataset.jumpNonce === String(item.jump.nonce)) return;
    node.dataset.jumpNonce = String(item.jump.nonce);
    const id = `jump:${item.tag}`;
    this.cancelAnimationById(node, id);
    const baseTransform = node.style.transform || 'translate(-50%, -50%)';
    const power = Number(item.jump.power) || 0;
    const offset = -power * unitScale;
    const duration = Math.max(0.001, Number(item.jump.seconds) || 1) * 1000;
    const iterations = item.jump.loop ? Infinity : Math.max(1, Number(item.jump.count) || 1);
    const animation = node.animate([
      { transform: baseTransform },
      { transform: `${baseTransform} translateY(${offset}px)` },
      { transform: baseTransform },
    ], { duration, iterations, easing: 'ease-in-out' });
    animation.id = id;
  }

  cancelAnimationById(node, id) {
    node.getAnimations().forEach((animation) => {
      if (animation.id === id) animation.cancel();
    });
  }

  sceneTargetForType(type, tag) {
    if (type === 'subimage') return this.target('subimages', tag || 'subimage');
    if (type === 'prefab' || type === 'prefabui') return this.target('prefabs', tag || 'prefab');
    if (type === 'chara' || type === 'character') return this.target('characters', tag || 'chara');
    return this.target('objects', tag || type || 'object');
  }

  applyLayerTransforms() {
    const rect = el.stage.getBoundingClientRect();
    const unitScale = Math.max(0.01, (rect.height || 1) / 2048);
    const camera = this.state.camera || {};
    const bg = this.state.backgroundTransform || {};
    const cameraTransform = `translate(${-Number(camera.x || 0) * unitScale}px, ${Number(camera.y || 0) * unitScale}px) scale(${Number(camera.zoom) || 1})`;
    const cameraTransition = `transform ${Math.max(0, Number(camera.seconds) || 0) * 1000}ms ${camera.easing || 'linear'}`;
    for (const layer of [el.live2dCanvas, el.mosaicLayer, el.fallbackTexture, el.sceneLayer]) {
      layer.style.transition = mergeTransition(layer.style.transition, cameraTransition);
      layer.style.transformOrigin = '50% 50%';
      layer.style.transform = cameraTransform;
    }
    const blurScale = el.stage.classList.contains('background-blur-active') ? 1.035 : 1;
    const bgTransform = `${cameraTransform} translate(${Number(bg.x || 0) * unitScale}px, ${-Number(bg.y || 0) * unitScale}px) scale(${(Number(bg.scaleX) || 1) * blurScale}, ${(Number(bg.scaleY) || 1) * blurScale})`;
    const bgTransition = `transform ${Math.max(0, Number(bg.seconds || camera.seconds) || 0) * 1000}ms ${bg.easing || camera.easing || 'linear'}`;
    el.bgLayer.style.transition = mergeTransition(el.bgLayer.style.transition, bgTransition);
    el.bgLayer.style.transformOrigin = '50% 50%';
    el.bgLayer.style.transform = bgTransform;
  }

  verifyBackgroundImage(backgroundName) {
    const image = new Image();
    image.onerror = () => setStatus(`background asset missing: ${backgroundName}`, true);
    image.src = `${BG_DATA_ROOT}${backgroundName}.png`;
  }

  render() {
    const activeKeys = new Set();
    const stageRect = el.stage.getBoundingClientRect();
    const unitScale = Math.max(0.01, (stageRect.height || 1) / 2048);
    this.applyLayerTransforms();

    Object.values(this.state.characters).forEach((item, index) => {
      if (item.deleted || !item.loaded) return;
      this.renderCharacter(item, index, stageRect, unitScale, activeKeys);
    });

    [
      ...Object.values(this.state.objects),
      ...Object.values(this.state.stills),
      ...Object.values(this.state.subimages),
      ...Object.values(this.state.prefabs),
    ].filter((item) => item.visible !== false && !item.deleted).forEach((item) => {
      this.renderToken(item, activeKeys, stageRect, unitScale);
    });

    for (const child of Array.from(el.sceneLayer.children)) {
      if (!activeKeys.has(child.dataset.sceneKey)) child.remove();
    }
  }

  renderToken(item, activeKeys, stageRect, unitScale) {
    const key = `token:${item.kind || 'scene'}:${item.tag}`;
    if (!item.imageSrc) {
      el.sceneLayer.querySelector(`[data-scene-key="${cssEscape(key)}"]`)?.remove();
      return;
    }
    activeKeys.add(key);
    let node = el.sceneLayer.querySelector(`[data-scene-key="${cssEscape(key)}"]`);
    if (!node) {
      node = document.createElement('div');
      node.className = 'scene-token';
      node.dataset.sceneKey = key;
      el.sceneLayer.appendChild(node);
    }
    const x = (stageRect.width || 1) * 0.5 + (Number(item.x) || 0) * unitScale;
    const y = (stageRect.height || 1) * 0.5 - (Number(item.y) || 0) * unitScale;
    const scale = clamp(Number(item.scale) || 1, .2, 4);
    node.title = item.tag;
    node.classList.toggle('image-token', !!item.imageSrc);
    node.classList.toggle('full-frame-token', !!item.fullFrame);
    if (item.imageSrc && !item.fullFrame) {
      const baseWidth = Math.max(1, Number(item.assetWidth) || 320);
      const baseHeight = Math.max(1, Number(item.assetHeight) || 180);
      node.style.width = `${baseWidth * unitScale}px`;
      node.style.height = `${baseHeight * unitScale}px`;
    } else {
      node.style.width = '';
      node.style.height = '';
    }
    node.style.left = `${x}px`;
    node.style.top = `${y}px`;
    node.style.opacity = String(clamp(Number(item.alpha ?? 1), 0, 1));
    node.style.zIndex = String(80 + (Number(item.priority) || 0));
    node.style.transitionDuration = `${Math.max(0, Number(item.transitionSeconds) || 0) * 1000}ms`;
    node.style.transitionTimingFunction = item.easing || 'linear';
    node.style.backgroundImage = item.imageSrc ? `url("${item.imageSrc}")` : '';
    node.textContent = item.imageSrc ? '' : (item.stillIndex ? `${item.asset || item.tag || ''} #${item.stillIndex}` : (item.asset || item.tag || ''));
    node.style.transform = item.fullFrame
      ? `translate(-50%, -50%) rotate(${Number(item.rotation) || 0}deg)`
      : `translate(-50%, -50%) scale(${Number(item.scaleX) || scale}, ${Number(item.scaleY) || scale}) rotate(${Number(item.rotation) || 0}deg)`;
    if (item.shake?.nonce && node.dataset.shakeNonce !== String(item.shake.nonce)) {
      node.dataset.shakeNonce = String(item.shake.nonce);
      this.applyShakeToElement(node, `shake:${item.tag}`, item.shake);
    }
    this.applyJumpToElement(node, item, unitScale);
  }

  renderCharacter(item, index, stageRect, unitScale, activeKeys) {
    const key = `character:${item.tag}`;
    activeKeys.add(key);
    let node = el.sceneLayer.querySelector(`[data-scene-key="${cssEscape(key)}"]`);
    if (!node) {
      node = document.createElement('div');
      node.className = 'scene-character';
      node.dataset.sceneKey = key;
      node.innerHTML = `
        <div class="scene-character-body">
          <img class="scene-character-body-image" alt="">
          <img class="scene-character-face-image" alt="">
        </div>
        <div class="scene-character-emotion"></div>
      `;
      el.sceneLayer.appendChild(node);
    }

    const rootRect = item.asset.rootRect || {};
    const rootSize = rectSize(rootRect, 2048, 2048);
    const rootWidth = rootSize.x * unitScale;
    const rootHeight = rootSize.y * unitScale;
    const stageCenterX = (stageRect.width || 1) * 0.5;
    const stageCenterY = (stageRect.height || 1) * 0.5;
    const finalX = stageCenterX + (Number(item.x) || 0) * unitScale;
    const finalY = stageCenterY - (Number(item.y) || 0) * unitScale;
    const seconds = Math.max(0, Number(item.transitionSeconds) || 0);
    const timing = item.easing || 'linear';
    const alpha = item.visible === false ? 0 : clamp(Number(item.alpha) || 0, 0, 1);

    node.dataset.characterId = item.characterId || '';
    node.dataset.face = currentCharaFaceKey(item);
    node.dataset.emotion = item.emotion || '';
    node.style.width = `${rootWidth}px`;
    node.style.height = `${rootHeight}px`;
    node.style.left = `${finalX}px`;
    node.style.top = `${finalY}px`;
    node.style.opacity = String(alpha);
    node.style.zIndex = String(40 + index);
    node.style.filter = characterFilter(item);
    node.style.transitionDuration = `${seconds * 1000}ms`;
    node.style.transitionTimingFunction = timing;
    node.style.transform = `translate(-50%, -50%) scale(${clamp(Number(item.scale) || 1, 0.05, 5)})`;

    this.renderCharacterBody(node, item, unitScale);
    this.renderCharacterEmotion(node, item, unitScale);
    this.applyCharacterReaction(node, item);
    this.applyJumpToElement(node, item, unitScale);
  }

  renderCharacterBody(node, item, unitScale) {
    const body = node.querySelector('.scene-character-body');
    const bodyImage = node.querySelector('.scene-character-body-image');
    const faceImage = node.querySelector('.scene-character-face-image');
    const bodyRect = item.asset.bodyRect || {};
    const faceRect = item.asset.faceContentRect || {};
    const bodySize = rectSize(bodyRect, 1024, 1600);
    const bodyPosition = rectPosition(bodyRect);
    const bodyScale = rectScale(bodyRect, 1);
    const bodyPivot = rectPivot(bodyRect);
    const bodyWidth = bodySize.x * unitScale;
    const bodyHeight = bodySize.y * unitScale;

    body.style.width = `${bodyWidth}px`;
    body.style.height = `${bodyHeight}px`;
    body.style.left = `calc(50% + ${bodyPosition.x * unitScale}px)`;
    body.style.top = `calc(50% + ${-bodyPosition.y * unitScale}px)`;
    body.style.transform = `translate(${-bodyPivot.x * 100}%, ${-(1 - bodyPivot.y) * 100}%) scale(${bodyScale.x}, ${bodyScale.y})`;

    const bodySrc = `${item.assetBase}${item.asset.files.body}`;
    if (bodyImage.dataset.src !== bodySrc) {
      bodyImage.src = bodySrc;
      bodyImage.dataset.src = bodySrc;
    }

    const faceSrc = this.characterFaceUrl(item);
    if (faceSrc) {
      const faceSize = rectSize(faceRect, 256, 256);
      const facePosition = rectPosition(faceRect);
      faceImage.hidden = false;
      faceImage.style.width = `${faceSize.x * unitScale}px`;
      faceImage.style.height = `${faceSize.y * unitScale}px`;
      faceImage.style.left = `calc(50% + ${facePosition.x * unitScale}px)`;
      faceImage.style.top = `calc(50% + ${-facePosition.y * unitScale}px)`;
      if (faceImage.dataset.src !== faceSrc) {
        faceImage.src = faceSrc;
        faceImage.dataset.src = faceSrc;
      }
    } else {
      faceImage.hidden = true;
      faceImage.removeAttribute('src');
      faceImage.dataset.src = '';
    }
  }

  renderCharacterEmotion(node, item, unitScale) {
    const emotionNode = node.querySelector('.scene-character-emotion');
    const emotion = normalizeAssetKey(item.emotion);
    if (!emotion) {
      emotionNode.hidden = true;
      emotionNode.replaceChildren();
      emotionNode.dataset.emotion = '';
      return;
    }

    const asset = app.emotionAssets.get(emotion);
    const emotionRect = item.asset.emotionRect || {};
    const attachment = emotionAttachmentTransform(item.asset);
    const prefabRootRect = asset?.rootRect || {};
    const prefabRootPosition = rectPosition(prefabRootRect);
    const prefabRootScale = rectScale(prefabRootRect, 1);
    const position = {
      x: attachment.position.x + prefabRootPosition.x,
      y: attachment.position.y + prefabRootPosition.y,
    };
    const scale = {
      x: attachment.scale.x * prefabRootScale.x,
      y: attachment.scale.y * prefabRootScale.y,
    };
    const pivot = rectPivot(prefabRootRect);
    const rootSize = rectSize(asset?.rootRect || emotionRect, 150, 150);
    emotionNode.hidden = false;
    emotionNode.dataset.emotion = emotion;
    emotionNode.dataset.nonce = String(item.emotionNonce || 0);
    emotionNode.style.width = `${rootSize.x * unitScale}px`;
    emotionNode.style.height = `${rootSize.y * unitScale}px`;
    emotionNode.style.left = `calc(50% + ${position.x * unitScale}px)`;
    emotionNode.style.top = `calc(50% + ${-position.y * unitScale}px)`;
    emotionNode.style.transform = `translate(${-pivot.x * 100}%, ${-(1 - pivot.y) * 100}%) scale(${scale.x}, ${scale.y})`;

    if (asset?.parts?.length) {
      if (emotionNode.dataset.renderKey !== `${emotion}:${asset.parts.length}`) {
        emotionNode.replaceChildren(...asset.parts.map((part, index) => createEmotionPart(part, index)));
        emotionNode.dataset.renderKey = `${emotion}:${asset.parts.length}`;
      }
      for (const child of Array.from(emotionNode.children)) {
        const part = asset.parts[Number(child.dataset.partIndex) || 0];
        applyEmotionPartLayout(child, part, unitScale);
      }
    } else {
      const fallbackSrc = `${CHARA_EMOTION_ROOT}${emotion}.png`;
      if (emotionNode.dataset.renderKey !== `${emotion}:fallback`) {
        const image = document.createElement('img');
        image.className = 'scene-character-emotion-part';
        image.alt = '';
        image.dataset.partIndex = '0';
        emotionNode.replaceChildren(image);
        emotionNode.dataset.renderKey = `${emotion}:fallback`;
      }
      const image = emotionNode.firstElementChild;
      const fallbackPart = {
        file: `${emotion}.png`,
        position: { x: 0, y: 0 },
        size: rootSize,
        pivot: { x: 0.5, y: 0.5 },
        localScale: { x: 1, y: 1 },
      };
      if (image.dataset.src !== fallbackSrc) {
        image.src = fallbackSrc;
        image.dataset.src = fallbackSrc;
      }
      applyEmotionPartLayout(image, fallbackPart, unitScale);
    }
  }

  applyCharacterReaction(node, item) {
    const nonce = String(item.reactionNonce || 0);
    if (!item.reaction || node.dataset.reactionNonce === nonce) return;
    node.dataset.reactionNonce = nonce;
    const seconds = Math.max(0.1, Number(item.reactionSeconds) || 1);
    const duration = seconds * 1000;
    if (item.reaction === 'jump') {
      node.animate([
        { transform: node.style.transform },
        { transform: `${node.style.transform} translateY(-34px)` },
        { transform: node.style.transform },
      ], { duration, easing: 'ease-out' });
    } else if (item.reaction === 'nod') {
      node.animate([
        { transform: node.style.transform },
        { transform: `${node.style.transform} translateY(18px)` },
        { transform: node.style.transform },
      ], { duration, easing: 'ease-in-out' });
    } else if (item.reaction === 'shake') {
      node.animate([
        { transform: `${node.style.transform} translateX(0)` },
        { transform: `${node.style.transform} translateX(-24px)` },
        { transform: `${node.style.transform} translateX(24px)` },
        { transform: `${node.style.transform} translateX(0)` },
      ], { duration, easing: 'ease-in-out' });
    }
  }

  characterFaceUrl(item) {
    const faces = item.asset?.faces || {};
    const faceKey = currentCharaFaceKey(item);
    const path = faces[faceKey] || faces[item.baseFace] || faces.normal || Object.values(faces)[0];
    return path ? `${item.assetBase}${path}` : '';
  }
}

class NovelModelSound {
  constructor() {
    this.elements = new Map();
    this.mediaSources = new WeakMap();
    this.voiceAnalysers = new Map();
    this.storyCues = {};
    this.globalCueSources = [];
    this.basePath = '';
    this.unlocked = false;
    this.blocked = false;
    this.audioContext = null;
    this.pendingVoices = new Map();
  }

  loadStory(story, basePath) {
    this.stopAll();
    this.storyCues = story.audio?.cues || {};
    this.globalCueSources = app.globalAudio?.sources || [];
    this.basePath = basePath;
    this.pendingVoices.clear();
  }

  unlock() {
    this.unlocked = true;
    this.blocked = false;
    try { this.ensureAudioContext()?.resume?.(); } catch (_) {}
    const currentVoice = app.controller?.message?.state?.voiceCue || '';
    for (const [key, audio] of this.elements.entries()) {
      if (!audio.src || !audio.paused) continue;
      if (audio.ended || audio.dataset.status === 'playend' || audio.dataset.status === 'stop' || audio.dataset.status === 'error') continue;
      const isLoop = audio.dataset.loop === 'true';
      const isCurrentVoice = key.startsWith('voice:') && audio.dataset.cue === currentVoice;
      if (isLoop || isCurrentVoice) {
        audio.dataset.status = 'prep';
        audio.play()
          .then(() => {
            if (!audio.paused && !audio.ended) audio.dataset.status = 'playing';
          })
          .catch((error) => {
            const blocked = error?.name === 'NotAllowedError';
            if (blocked) this.blocked = true;
            audio.dataset.status = blocked ? 'blocked' : (audio.error ? 'error' : 'prep');
          });
      }
    }
  }

  play(cueName, channel, tag, options = {}) {
    const resolved = this.resolveCue(cueName);
    if (!resolved) {
      if (cueName) setStatus(`missing cue: ${cueName}`, true);
      return;
    }
    const { cue, basePath } = resolved;
    const key = `${channel}:${tag || channel}`;
    let audio = this.elements.get(key);
    if (!audio) {
      audio = new Audio();
      audio.preload = 'auto';
      this.installAudioStateHandlers(audio);
      this.elements.set(key, audio);
    }
    audio.pause();
    audio.src = `${basePath}${cue.path}`;
    audio.dataset.fallbackSrc = cue.fallbackPath ? `${basePath}${cue.fallbackPath}` : '';
    audio.dataset.directSe = cue.directSe ? 'true' : 'false';
    const targetVolume = clamp(options.volume ?? 1, 0, 1);
    const fadeInSeconds = Math.max(0, Number(options.fadeInSeconds) || 0);
    audio.volume = fadeInSeconds > 0 ? 0 : targetVolume;
    audio.loop = Boolean(options.loop);
    audio.dataset.loop = String(audio.loop);
    audio.dataset.channel = channel;
    audio.dataset.tag = tag || channel;
    audio.dataset.key = key;
    audio.dataset.baseVolume = String(targetVolume);
    audio.dataset.cue = cueName;
    audio.dataset.status = 'prep';
    audio.dataset.endedAt = '';
    audio.dataset.startedAt = String(performance.now());
    audio.dataset.triedFallback = 'false';
    audio.currentTime = 0;
    if (channel === 'voice') {
      try { this.attachVoiceAnalyser(key, audio); }
      catch (error) {
        // Web Audio analysis is optional; do not prevent the audio element
        // from playing when analyser/source creation is unavailable.
        console.warn('[lip-sync] voice analyser unavailable; using audio-clock fallback', error);
      }
    }
    try { this.audioContext?.resume?.(); } catch (_) {}
    audio.play().catch((error) => {
      const blocked = error?.name === 'NotAllowedError';
      if (blocked) this.blocked = true;
      audio.dataset.status = blocked ? 'blocked' : (audio.error ? 'error' : 'prep');
      if (blocked) setStatus('click to enable audio');
      else if (audio.error) setStatus(`${cueName}: ${error?.message || error}`, true);
    });
    if (fadeInSeconds > 0) fadeAudio(audio, targetVolume, fadeInSeconds);
    return audio;
  }

  queueVoice(cueName, enabled = true) {
    const cue = String(cueName || '');
    if (!cue) return;
    this.pendingVoices.set(cue, Boolean(enabled));
  }

  consumeMessageVoice(preferredCue = '') {
    const cue = String(preferredCue || '');
    if (cue) {
      if (this.pendingVoices.has(cue) && !this.pendingVoices.get(cue)) {
        this.pendingVoices.delete(cue);
        return '';
      }
      this.pendingVoices.delete(cue);
      return cue;
    }
    for (const [pendingCue, enabled] of this.pendingVoices.entries()) {
      this.pendingVoices.delete(pendingCue);
      if (enabled) return pendingCue;
    }
    return '';
  }

  preloadCue(cueName) {
    const resolved = this.resolveCue(cueName);
    if (!resolved) return false;
    const audio = new Audio();
    audio.preload = 'auto';
    audio.src = `${resolved.basePath}${resolved.cue.path}`;
    audio.load();
    return true;
  }

  resolveCue(cueName) {
    const exactName = String(cueName || '');
    const safeName = normalizeAudioCueKey(exactName);
    const storyCue = this.storyCues[exactName] || this.storyCues[safeName];
    if (storyCue) return { cue: storyCue, basePath: this.basePath };
    const sources = [...this.globalCueSources].sort((left, right) => {
      const leftCurrent = this.basePath.startsWith(left.basePath) ? 0 : 1;
      const rightCurrent = this.basePath.startsWith(right.basePath) ? 0 : 1;
      return leftCurrent - rightCurrent;
    });
    for (const source of sources) {
      const globalCue = source.cues?.[exactName] || source.cues?.[safeName];
      if (globalCue) return { cue: globalCue, basePath: source.basePath };
    }
    // SE 可直接放在 data_r18_all/audio/se/decoded/misc，无需 index.json。
    if (/^se[\w-]+$/i.test(exactName)) {
      return { cue: { name: exactName, category: 'se', path: `audio/se/decoded/misc/${exactName}.wav`, fallbackPath: `audio/se/decoded/misc/${exactName}.ogg`, duration: 0, directSe: true }, basePath: R18_DATA_ROOT };
    }
    return null;
  }

  cueDuration(cueName) {
    const resolved = this.resolveCue(cueName);
    return Number(resolved?.cue?.duration) || 0;
  }

  installAudioStateHandlers(audio) {
    audio.addEventListener('playing', () => {
      audio.dataset.status = 'playing';
    });
    audio.addEventListener('waiting', () => {
      if (!audio.ended) audio.dataset.status = 'prep';
    });
    audio.addEventListener('stalled', () => {
      if (!audio.ended) audio.dataset.status = 'prep';
    });
    audio.addEventListener('ended', () => {
      audio.dataset.status = 'playend';
      audio.dataset.endedAt = String(performance.now());
      this.cleanupLiveChannelForAudio(audio);
    });
    audio.addEventListener('pause', () => {
      if (!audio.ended && audio.dataset.status !== 'prep' && audio.dataset.status !== 'blocked') {
        audio.dataset.status = 'stop';
      }
    });
    audio.addEventListener('error', () => {
      if (audio.dataset.fallbackSrc && audio.src !== audio.dataset.fallbackSrc && audio.dataset.triedFallback !== 'true') {
        audio.dataset.triedFallback = 'true';
        audio.src = audio.dataset.fallbackSrc;
        audio.load();
        audio.play().catch(() => {
          audio.dataset.status = 'error';
        });
        return;
      }
      if (audio.dataset.directSe === 'true') setStatus(`missing cue: ${audio.dataset.cue}`, true);
      audio.dataset.status = 'error';
      this.cleanupLiveChannelForAudio(audio);
    });
  }

  cleanupLiveChannelForAudio(audio) {
    const tag = audio.dataset.tag || '';
    if (!tag || !window._liveChannels?.[tag]) return;
    const info = window._liveChannels[tag];
    if (info.audio && info.audio !== audio) return;
    if (audio.loop || info.type === 'bgm') return;
    delete window._liveChannels[tag];
  }

  setLiveVolume(type, factor) {
    const targetType = String(type || '');
    const multiplier = clamp(Number(factor) || 0, 0, 1);
    const handled = new Set();
    if (window._liveChannels) {
      for (const [tag, info] of Object.entries(window._liveChannels)) {
        if (!info || info.type !== targetType) continue;
        const audio = info.audio
          || this.elements.get(info.key || `${info.channel || targetType}:${tag}`)
          || this.findLiveAudio(tag, targetType);
        if (!this.isLiveAudioActive(audio)) {
          delete window._liveChannels[tag];
          continue;
        }
        audio.volume = clamp((Number(info.origVolume) || 0) * multiplier, 0, 1);
        handled.add(audio);
      }
    }
    for (const audio of this.elements.values()) {
      if (handled.has(audio)) continue;
      if (!this.isLiveAudioActive(audio)) continue;
      if (this.volumeTypeForAudio(audio) !== targetType) continue;
      const base = Number(audio.dataset.baseVolume);
      audio.volume = clamp((Number.isFinite(base) ? base : audio.volume) * multiplier, 0, 1);
    }
  }

  isLiveAudioActive(audio) {
    return !!(audio?.src && !audio.ended && audio.dataset.status !== 'playend' && audio.dataset.status !== 'stop' && audio.dataset.status !== 'error');
  }

  findLiveAudio(tag, type) {
    for (const audio of this.elements.values()) {
      if (audio.dataset.tag !== tag) continue;
      if (this.volumeTypeForAudio(audio) !== type) continue;
      return audio;
    }
    return null;
  }

  volumeTypeForAudio(audio) {
    const channel = audio?.dataset?.channel || '';
    return channel === 'bgv' ? 'bgm' : channel;
  }

  ensureAudioContext() {
    if (this.audioContext) return this.audioContext;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return null;
    this.audioContext = new AudioContextClass();
    return this.audioContext;
  }

  attachVoiceAnalyser(key, audio) {
    const context = this.ensureAudioContext();
    if (!context) return;
    let item = this.mediaSources.get(audio);
    if (!item) {
      const source = context.createMediaElementSource(audio);
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      // Keep analyser smoothing low; envelope smoothing is handled below so
      // syllable attacks remain responsive.
      analyser.smoothingTimeConstant = 0.06;
      source.connect(analyser);
      analyser.connect(context.destination);
      item = {
        source,
        analyser,
        data: new Float32Array(analyser.frequencyBinCount),
        timeData: new Uint8Array(analyser.fftSize),
        audio,
        lipSyncEnvelope: {
          noiseFloor: L2D_LIP_SYNC.noiseFloorMin,
          speechPeak: L2D_LIP_SYNC.adaptivePeakFloor,
          spectralVoiced: 0.5,
          spectralBrightness: 0.35,
          spectralConsonant: 0,
          lastNow: performance.now(),
        },
      };
      this.mediaSources.set(audio, item);
    }
    this.voiceAnalysers.set(key, item);
  }

  currentVoiceAnalyser() {
    for (const [key, item] of this.voiceAnalysers.entries()) {
      if (!key.startsWith('voice:')) continue;
      if (item.audio?.src && !item.audio.paused && !item.audio.ended) return item;
    }
    return null;
  }

  currentVoiceTimeSeconds() {
    const item = this.currentVoiceAnalyser();
    return item?.audio ? item.audio.currentTime : 0;
  }

  currentVoiceLengthSeconds() {
    const item = this.currentVoiceAnalyser();
    if (!item?.audio) return 0;
    if (Number.isFinite(item.audio.duration)) return item.audio.duration;
    const cueName = item.audio.dataset.cue;
    return this.cueDuration(cueName);
  }

  currentVoiceLipFeatures() {
    const item = this.currentVoiceAnalyser();
    if (!item) {
      // Keep lip-sync working if Web Audio analysis is unavailable or the
      // analyser has not attached yet. The original app used the playing
      // audio element's clock for this fallback.
      const audio = Array.from(this.elements.values()).find((candidate) =>
        candidate?.dataset?.channel === 'voice' && candidate.src && !candidate.paused && !candidate.ended
      );
      return {
        opening: audio ? this.syntheticVoiceMouthOpening(audio) : 0,
        voiced: 0,
        brightness: 0.35,
        consonant: 0,
      };
    }
    try { item.analyser.getByteTimeDomainData(item.timeData); } catch (_) {}
    try { item.analyser.getFloatFrequencyData(item.data); } catch (_) {}
    const now = performance.now();
    const envelope = item.lipSyncEnvelope || (item.lipSyncEnvelope = {
      noiseFloor: L2D_LIP_SYNC.noiseFloorMin,
      speechPeak: L2D_LIP_SYNC.adaptivePeakFloor,
      spectralVoiced: 0.5,
      spectralBrightness: 0.35,
      spectralConsonant: 0,
      lastNow: now,
    });
    const dt = Math.min(0.08, Math.max(0.001, (now - Number(envelope.lastNow || now)) / 1000));
    envelope.lastNow = now;
    const adaptiveOpeningRaw = adaptiveVoiceMouthOpening(item.timeData, envelope, dt);
    const adaptiveOpening = Number.isFinite(Number(adaptiveOpeningRaw))
      ? clamp(Number(adaptiveOpeningRaw), 0, 1)
      : 0;
    // Retain the original app.js RMS detector as a floor. The adaptive
    // spectral detector can otherwise classify quiet speech as silence.
    let sumSquares = 0;
    for (let i = 0; i < item.timeData.length; i += 1) {
      const sample = (Number(item.timeData[i]) - 128) / 128;
      sumSquares += sample * sample;
    }
    const rmsOpening = clamp(Math.sqrt(sumSquares / Math.max(1, item.timeData.length)) * 5.5, 0, 1);
    const opening = Math.max(adaptiveOpening, rmsOpening);
    const spectrum = analyzeVoiceSpectrum(
      item.data,
      Number(this.audioContext?.sampleRate) || 48000,
      Number(item.analyser.fftSize) || 1024,
      envelope,
      dt,
    );
    return { opening, ...spectrum };
  }

  currentVoiceMouthOpening() {
    return this.currentVoiceLipFeatures().opening;
  }

  syntheticVoiceMouthOpening(audio) {
    if (!audio || audio.paused || audio.ended) return 0;
    const time = Math.max(0, Number(audio.currentTime) || 0);
    const duration = Number.isFinite(audio.duration) ? audio.duration : this.cueDuration(audio.dataset.cue);
    const fadeIn = clamp(time / 0.12, 0, 1);
    const fadeOut = duration > 0 ? clamp((duration - time) / 0.18, 0, 1) : 1;
    const envelope = Math.min(fadeIn, fadeOut);
    const syllable = 0.5 + 0.5 * Math.sin(time * 32);
    const phrase = 0.5 + 0.5 * Math.sin(time * 11 + 0.8);
    return envelope * L2D_LIP_SYNC.maxOpening * (0.18 + 0.42 * syllable * phrase);
  }

  fadeChannel(channel, tag, volume, seconds) {
    const targetVolume = clamp(volume, 0, 1);
    const duration = Math.max(0, Number(seconds) || 0);
    const tagText = String(tag || '');
    const keyPrefix = `${channel}:${tagText}`;
    const allTags = !tagText || tagText.toUpperCase() === 'ALL';
    for (const [key, audio] of this.elements.entries()) {
      if (allTags) {
        if (!key.startsWith(`${channel}:`)) continue;
      } else if (key !== keyPrefix) {
        continue;
      }
      if (!audio.src || audio.ended || audio.dataset.status === 'playend') continue;
      fadeAudio(audio, targetVolume, duration, () => {
        if (targetVolume <= 0) {
          audio.pause();
          audio.currentTime = 0;
        }
      });
    }
  }

  fadeStop(channel, tag, seconds) {
    const keyPrefix = `${channel}:${tag || ''}`;
    for (const [key, audio] of this.elements.entries()) {
      if (tag && key !== keyPrefix) continue;
      if (!tag && !key.startsWith(`${channel}:`)) continue;
      fadeAudio(audio, 0, seconds, () => {
        audio.pause();
        audio.currentTime = 0;
      });
    }
  }

  stopVoiceAll() {
    this.fadeStop('voice', '', 0);
    this.voiceAnalysers.clear();
    this.pendingVoices.clear();
    if (window._liveChannels) delete window._liveChannels.voice;
  }

  stopAll() {
    for (const audio of this.elements.values()) {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    }
    this.elements.clear();
    this.voiceAnalysers.clear();
    this.pendingVoices.clear();
    if (window._liveChannels) window._liveChannels = {};
  }

  currentLabels() {
    return Array.from(this.elements.entries())
      .filter(([, audio]) => audio.src && !audio.paused)
      .map(([key, audio]) => `${key}:${audio.dataset.cue || '?'}`);
  }

  isVoicePendingOrPlaying(cueName = '') {
    for (const [key, audio] of this.elements.entries()) {
      if (!key.startsWith('voice:')) continue;
      if (cueName && audio.dataset.cue !== cueName) continue;
      const status = audio.dataset.status || '';
      if (status === 'blocked' || status === 'error' || status === 'playend' || status === 'stop') continue;
      if (status === 'prep' || status === 'playing') return true;
      if (audio.src && !audio.ended && audio.readyState < HTMLMediaElement.HAVE_ENOUGH_DATA) return true;
      if (audio.src && !audio.ended && !audio.paused) return true;
    }
    return false;
  }

  voiceEndedAt(cueName = '') {
    let endedAt = 0;
    for (const [key, audio] of this.elements.entries()) {
      if (!key.startsWith('voice:')) continue;
      if (cueName && audio.dataset.cue !== cueName) continue;
      if (audio.dataset.status !== 'playend') continue;
      endedAt = Math.max(endedAt, Number(audio.dataset.endedAt) || 0);
    }
    return endedAt;
  }

  remainingVoiceMs(cueName = '') {
    let remaining = 0;
    for (const [key, audio] of this.elements.entries()) {
      if (!key.startsWith('voice:')) continue;
      if (cueName && audio.dataset.cue !== cueName) continue;
      if (!this.isVoicePendingOrPlaying(audio.dataset.cue || '')) continue;
      const cueDuration = this.cueDuration(audio.dataset.cue);
      const duration = Number.isFinite(audio.duration) ? audio.duration : cueDuration;
      if (duration > 0) remaining = Math.max(remaining, Math.max(0, duration - audio.currentTime) * 1000);
      else remaining = Math.max(remaining, AUTO_CHECK_MS);
    }
    return remaining;
  }
}

class NovelModelLive2D {
  constructor() {
    this.app = null;
    this.model = null;
    this.canvasClip = null;
    this.ready = false;
    this.visible = false;
    this.hasFallbackTexture = false;
    this.basePath = '';
    this.motionMap = new Map();
    this.groupParameterIds = new Map();
    this.mouthParameterIds = new Set();
    this.authoredParameterRanges = new Map();
    this.motionGeneration = new Map();
    this.motionQueues = new Map();
    this.parameterLayers = new Map();
    // Reference value of each authored curve at t=0. Non-Scene motions are
    // applied as deltas from these authored starting values.
    this.motionLayerReferences = new Map();
    this.lastMotionByGroup = new Map();
    this.activeMotionByGroup = new Map();
    this.partOpacityLayers = new Map();
    this.partOpacityDefaults = new Map();
    this.modelEffectLayers = new Map();
    this.heldParameters = new Map();
    this.appliedParameterIds = new Set();
    this.appliedPartOpacityIds = new Set();
    this.holdFrame = 0;
    this.live2dMotionUpdateHandler = null;
    this.live2dUpdateHandler = null;
    this.live2dUpdateHookInstalled = false;
    this.currentMotion = '';
    this.lipSync = null;
    this.lipSyncMode = 'multi';
    this.lipSyncModeMulti = true;
    this.mosaicMode = 'original';
    this.mosaicDrawables = [];
    this.backgroundDrawables = [];
    this.backgroundIndexSet = new Set();
    this.mosaicScratch = document.createElement('canvas');
    this.mosaicStatus = 'uninitialized';
    this.mosaicDrawPatchInstalled = false;
    this.compatTextureObjects = [];
  }

  reset() {
    this.ready = false;
    this.visible = false;
    this.hasFallbackTexture = false;
    this.currentMotion = '';
    this.motionMap.clear();
    this.groupParameterIds.clear();
    this.mouthParameterIds.clear();
    this.authoredParameterRanges.clear();
    this.motionGeneration.clear();
    this.motionQueues.clear();
    this.parameterLayers.clear();
    this.motionLayerReferences.clear();
    this.lastMotionByGroup.clear();
    this.activeMotionByGroup.clear();
    this.partOpacityLayers.clear();
    this.partOpacityDefaults.clear();
    this.modelEffectLayers.clear();
    this.heldParameters.clear();
    this.appliedParameterIds.clear();
    this.appliedPartOpacityIds.clear();
    this.lipSync = null;
    this.lipSyncMode = 'multi';
    this.lipSyncModeMulti = true;
    this.mosaicDrawables = [];
    this.backgroundDrawables = [];
    this.backgroundIndexSet.clear();
    this.mosaicStatus = 'uninitialized';
    this.mosaicDrawPatchInstalled = false;
    for (const texture of this.compatTextureObjects) {
      try { texture?.destroy?.(true); } catch (_) {}
    }
    this.compatTextureObjects = [];
    this.clearMosaicOverlay();
    if (this.holdFrame) cancelAnimationFrame(this.holdFrame);
    this.holdFrame = 0;
    this.uninstallLive2DUpdateHook();
    this.clearCanvasClip();
    if (this.model) {
      try { this.app?.stage?.removeChild(this.model); } catch (_) {}
      try { this.model.destroy(); } catch (_) {}
    }
    this.model = null;
    el.motionBadge.textContent = '';
    el.live2dCanvas.classList.remove('live2d-layer-visible');
    el.mosaicLayer.classList.remove('live2d-layer-visible');
    el.fallbackTexture.classList.remove('visible');
    el.fallbackTexture.removeAttribute('src');
    el.fallbackTexture.dataset.src = '';
  }

  async loadStory(story, basePath) {
    this.basePath = basePath;
    this.storyId = String(story?.id || story?.sourceId || '');
    this.buildMotionMap(story.live2d);
    await this.preloadMotionCurves();
    const fallback = story.live2d?.textures?.find((texture) => texture.name === 'texture_00') || story.live2d?.textures?.[0];
    if (fallback) {
      el.fallbackTexture.src = `${basePath}${fallback.path}`;
      el.fallbackTexture.dataset.src = el.fallbackTexture.src;
      this.hasFallbackTexture = true;
    }
    if (!story.live2d?.model3 || !window.PIXI?.live2d?.Live2DModel) {
      setStatus(this.hasFallbackTexture ? 'Live2D fallback texture' : '');
      return;
    }
    try {
      this.ensurePixi();
      const modelUrl = `${basePath}${story.live2d.model3}`;
      this.model = await promiseWithTimeout(
        window.PIXI.live2d.Live2DModel.from(modelUrl),
        LIVE2D_MODEL_LOAD_TIMEOUT_MS,
        `Live2D model load timeout: ${modelUrl}`,
      );
      this.model.visible = false;
      this.model.autoInteract = false;
      this.configureLive2DRendererAlpha();
      this.configureLive2DMaskQuality();
      this.normalizeLive2DTextureEdges();
      this.disableSdkIdleMotion();
      this.disableSdkNaturalMotion();
      this.app.stage.addChild(this.model);
      this.disableTextureMipmaps();
      this.ready = true;
      this.cacheMosaicDrawables();
      this.cacheBackgroundDrawables();
      this.installMosaicDrawableSuppression();
      // Parameter composition is driven by our own RAF hold loop. Do not hook
      // Cubism's internal update events here: some exported models/runtimes can
      // re-enter model updates and make loading appear to hang.
      this.ensureParameterHoldLoop();
      this.fit();
      setStatus('Live2D ready');
    } catch (error) {
      setStatus(`Live2D fallback: ${error?.message || error}`, true);
    }
  }

  fullscreenFrame() {
    const width = el.stage.clientWidth || 1;
    const height = el.stage.clientHeight || 1;
    return { left: 0, top: 0, width, height, right: width, bottom: height };
  }

  cacheBackgroundDrawables() {
    const internal = this.model?.internalModel;
    const coreDrawables = internal?.coreModel?._model?.drawables;
    const ids = coreDrawables?.ids || [];
    const orders = coreDrawables?.renderOrders || [];
    const canvasWidth = Number(internal?.originalWidth) || 0;
    const canvasHeight = Number(internal?.originalHeight) || 0;
    const canvasArea = canvasWidth * canvasHeight;
    if (!ids.length || canvasArea <= 0) {
      this.backgroundDrawables = [];
      this.backgroundIndexSet.clear();
      return;
    }
    const minimumOrder = Math.min(...Array.from(orders, (value) => Number(value) || 0));
    const keywordPattern = /(?:^|_)(?:bg|background)|bed|futon|mattress|pillow|room|floor|wall|sky|window|curtain/i;
    const candidates = [];
    for (let index = 0; index < ids.length; index += 1) {
      const order = Number(orders[index]) || 0;
      if (order > minimumOrder + 6) continue;
      const bounds = this.drawableBounds(index);
      if (!bounds) continue;
      const area = Math.max(0, bounds.width) * Math.max(0, bounds.height);
      const id = String(ids[index]);
      const keyword = keywordPattern.test(id);
      if (!keyword && area < canvasArea * 0.06) continue;
      candidates.push({ index, id, order, area, keyword });
    }
    const hasNamedBackground = candidates.some((item) => item.keyword);
    this.backgroundDrawables = hasNamedBackground || candidates.length >= 3 ? candidates : [];
    this.backgroundIndexSet = new Set(this.backgroundDrawables.map((item) => item.index));
  }

  updateCanvasClip() {
    const internal = this.model?.internalModel;
    const canvasWidth = Number(internal?.originalWidth);
    const canvasHeight = Number(internal?.originalHeight);
    const stageWidth = el.stage.clientWidth || 1;
    const stageHeight = el.stage.clientHeight || 1;
    if (!this.model || !window.PIXI?.Point ||
        !Number.isFinite(canvasWidth) || canvasWidth <= 0 ||
        !Number.isFinite(canvasHeight) || canvasHeight <= 0) {
      this.clearCanvasClip();
      return;
    }

    const corners = [
      this.model.toGlobal(new PIXI.Point(0, 0)),
      this.model.toGlobal(new PIXI.Point(canvasWidth, 0)),
      this.model.toGlobal(new PIXI.Point(0, canvasHeight)),
      this.model.toGlobal(new PIXI.Point(canvasWidth, canvasHeight)),
    ];
    const frame = app.aspect169 ? this.fullscreenFrame() : null;
    const rawLeft = frame?.left ?? Math.min(...corners.map((point) => point.x));
    const rawRight = frame?.right ?? Math.max(...corners.map((point) => point.x));
    const rawTop = frame?.top ?? Math.min(...corners.map((point) => point.y));
    const rawBottom = frame?.bottom ?? Math.max(...corners.map((point) => point.y));
    if (![rawLeft, rawRight, rawTop, rawBottom].every(Number.isFinite) ||
        rawRight <= rawLeft || rawBottom <= rawTop) {
      this.clearCanvasClip();
      return;
    }

    const left = clamp(rawLeft, 0, stageWidth);
    const right = clamp(rawRight, 0, stageWidth);
    const top = clamp(rawTop, 0, stageHeight);
    const bottom = clamp(rawBottom, 0, stageHeight);
    const clip = `inset(${top}px ${Math.max(0, stageWidth - right)}px ${Math.max(0, stageHeight - bottom)}px ${left}px)`;
    this.canvasClip = { left, right, top, bottom, css: clip };
    for (const canvas of [el.live2dCanvas, el.mosaicLayer]) {
      canvas.style.clipPath = clip;
      canvas.style.webkitClipPath = clip;
    }
  }

  clearCanvasClip() {
    this.canvasClip = null;
    for (const canvas of [el.live2dCanvas, el.mosaicLayer]) {
      canvas.style.clipPath = '';
      canvas.style.webkitClipPath = '';
    }
  }
  live2DRendererResolution() {
    const width = Math.max(1, Number(el.stage?.clientWidth) || Number(window.innerWidth) || 1);
    const height = Math.max(1, Number(el.stage?.clientHeight) || Number(window.innerHeight) || 1);
    const dpr = Math.max(1, Number(window.devicePixelRatio) || 1);
    const pixelBudget = Math.max(1, Number(LIVE2D_TEXTURE_COMPAT.rendererPixelBudget) || 2400000);
    const budgetResolution = Math.sqrt(pixelBudget / Math.max(1, width * height));
    return clamp(
      Math.min(dpr, budgetResolution, LIVE2D_TEXTURE_COMPAT.maxRendererResolution),
      LIVE2D_TEXTURE_COMPAT.minRendererResolution,
      LIVE2D_TEXTURE_COMPAT.maxRendererResolution,
    );
  }

  ensurePixi() {
    this.configurePixiLive2DTextureSampling();
    const width = Math.max(1, el.stage.clientWidth || 1);
    const height = Math.max(1, el.stage.clientHeight || 1);
    const resolution = this.live2DRendererResolution();
    if (this.app) {
      try { this.app.renderer.resolution = resolution; } catch (_) {}
      this.app.renderer.resize(width, height);
      return;
    }
    this.app = new PIXI.Application({
      view: el.live2dCanvas,
      resizeTo: el.stage,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      resolution,
      preserveDrawingBuffer: true,
      premultipliedAlpha: true,
    });
  }

  configureLive2DMaskQuality() {
    const internal = this.model?.internalModel;
    const renderer = internal?.renderer;
    if (!internal || !renderer || typeof renderer.setClippingMaskBufferSize !== 'function') return;

    let ids = [];
    try { ids = Array.from(internal.getDrawableIDs?.() || []); } catch (_) {}
    const hasMouthMask = ids.some((id) => /(?:mouth.*mask|oral|tongue)/i.test(String(id || '')));
    if (!hasMouthMask) return;

    const stagePixels = Math.max(1, (el.stage?.clientWidth || 1) * (el.stage?.clientHeight || 1));
    const deviceMemory = Math.max(1, Number(navigator.deviceMemory) || 8);
    const lowSpec = deviceMemory <= 4 || stagePixels < 1400000;
    const size = lowSpec
      ? LIVE2D_TEXTURE_COMPAT.mouthMaskBufferLow
      : LIVE2D_TEXTURE_COMPAT.mouthMaskBufferHigh;

    try {
      const current = Number(renderer.getClippingMaskBufferSize?.()) || 256;
      if (current !== size) renderer.setClippingMaskBufferSize(size);
      console.info(`[Live2D mask] mouth clipping buffer ${current} -> ${size}; rendererResolution=${this.live2DRendererResolution().toFixed(2)}`);
    } catch (error) {
      console.warn('[Live2D mask] clipping buffer adjustment skipped', error);
    }
  }

  configurePixiLive2DTextureSampling() {
    if (!window.PIXI) return;
    try {
      if (PIXI.settings && PIXI.MIPMAP_MODES) PIXI.settings.MIPMAP_TEXTURES = PIXI.MIPMAP_MODES.OFF;
      if (PIXI.settings && PIXI.SCALE_MODES) PIXI.settings.SCALE_MODE = PIXI.SCALE_MODES.LINEAR;
      if (PIXI.settings) PIXI.settings.ROUND_PIXELS = false;
    } catch (_) {}
  }

  disableTextureMipmaps() {
    for (const texture of this.model?.textures || []) {
      const baseTexture = texture?.baseTexture;
      if (!baseTexture) continue;
      try {
        if (window.PIXI?.MIPMAP_MODES) baseTexture.mipmap = PIXI.MIPMAP_MODES.OFF;
        if (window.PIXI?.WRAP_MODES) baseTexture.wrapMode = PIXI.WRAP_MODES.CLAMP;
        if (window.PIXI?.SCALE_MODES) baseTexture.scaleMode = PIXI.SCALE_MODES.LINEAR;
      } catch (_) {}
      try { baseTexture.update?.(); } catch (_) {}
    }
  }

  configureLive2DRendererAlpha() {
    // pixi-live2d-display's Cubism 4 renderer is authored for premultiplied
    // alpha. Keep that contract explicit: a PMA renderer + a texture that is
    // premultiplied exactly once. This is especially important for the very
    // dark, low-alpha pixels around mouth/skin atlas islands.
    try { this.model?.internalModel?.renderer?.setIsPremultipliedAlpha?.(true); } catch (_) {}
    const alphaModes = window.PIXI?.ALPHA_MODES;
    const unpackMode = alphaModes?.UNPACK ?? alphaModes?.PREMULTIPLY_ON_UPLOAD;
    for (const texture of this.model?.textures || []) {
      const baseTexture = texture?.baseTexture;
      if (!baseTexture) continue;
      try {
        if (unpackMode != null) baseTexture.alphaMode = unpackMode;
        if (window.PIXI?.WRAP_MODES) baseTexture.wrapMode = PIXI.WRAP_MODES.CLAMP;
        if (window.PIXI?.SCALE_MODES) baseTexture.scaleMode = PIXI.SCALE_MODES.LINEAR;
        if (window.PIXI?.MIPMAP_MODES) baseTexture.mipmap = PIXI.MIPMAP_MODES.OFF;
      } catch (_) {}
    }
  }

  normalizeLive2DTextureEdges() {
    if (!LIVE2D_TEXTURE_COMPAT.alphaEdgeFix || !window.PIXI || !this.model?.textures?.length) return;
    const replacements = [];
    for (let index = 0; index < this.model.textures.length; index += 1) {
      const texture = this.model.textures[index];
      const source = texture?.baseTexture?.resource?.source;
      if (!source) continue;
      try {
        const score = live2dTexturePremultiplyEdgeScore(source);
        if (!(score >= LIVE2D_TEXTURE_COMPAT.suspectScore)) continue;
        const canvas = createLive2DStraightAlphaCompatCanvas(source);
        if (!canvas) continue;
        const alphaModes = PIXI.ALPHA_MODES;
        const alphaMode = alphaModes?.UNPACK ?? alphaModes?.PREMULTIPLY_ON_UPLOAD;
        const options = {};
        if (alphaMode != null) options.alphaMode = alphaMode;
        if (PIXI.MIPMAP_MODES) options.mipmap = PIXI.MIPMAP_MODES.OFF;
        if (PIXI.WRAP_MODES) options.wrapMode = PIXI.WRAP_MODES.CLAMP;
        if (PIXI.SCALE_MODES) options.scaleMode = PIXI.SCALE_MODES.LINEAR;
        const baseTexture = PIXI.BaseTexture?.from
          ? PIXI.BaseTexture.from(canvas, options)
          : new PIXI.BaseTexture(canvas, options);
        const replacement = new PIXI.Texture(baseTexture);
        replacement._dotAlphaEdgeCompat = true;
        replacements.push({ index, replacement, score });
      } catch (_) {
        // Cross-origin/unsupported texture sources simply keep the original.
      }
    }
    for (const item of replacements) {
      this.model.textures[item.index] = item.replacement;
      this.compatTextureObjects.push(item.replacement);
      console.info(`[Live2D texture] alpha-edge compatibility texture=${item.index} score=${item.score.toFixed(3)}`);
    }
  }

  buildMotionMap(live2d) {
    this.motionMap.clear();
    this.groupParameterIds.clear();
    this.mouthParameterIds.clear();
    for (const sourceMotion of live2d?.motions || []) {
      const entry = {
        ...sourceMotion,
        group: resolveLive2DGroup(sourceMotion.group, sourceMotion.name || fileBase(sourceMotion.path)),
        data: null,
      };
      this.motionMap.set(normalizeKey(sourceMotion.name), entry);
      this.motionMap.set(normalizeKey(fileBase(sourceMotion.path)), entry);
    }
  }

  async preloadMotionCurves() {
    const unique = new Set(this.motionMap.values());
    await Promise.all(Array.from(unique, async (motion) => {
      if (!motion.path) return;
      try {
        motion.data = await fetchJson(`${this.basePath}${motion.path}`);
        if (this.storyId === '10310100012' && Array.isArray(motion.data?.Curves)) {
          let foundParam4 = false;
          motion.data.Curves = motion.data.Curves.filter((curve) => {
            if (curve?.Target !== 'Parameter' || curve.Id !== 'Param4') return true;
            if (foundParam4) return false;
            foundParam4 = true;
            return true;
          });
        }
        this.registerMotionParameters(motion);
      } catch (error) {
        motion.data = null;
      }
    }));
  }

  registerMotionParameters(motion) {
    const group = this.motionGroup(motion);
    if (!this.groupParameterIds.has(group)) this.groupParameterIds.set(group, new Set());
    const ids = this.groupParameterIds.get(group);
    for (const curve of motion.data?.Curves || []) {
      if (curve.Target === 'Parameter' && curve.Id) {
        ids.add(String(curve.Id));
        if (this.isMouthParameterId(curve.Id) || this.isMouthEmotionGroup(group)) {
          this.mouthParameterIds.add(String(curve.Id));
        }
      }
    }
  }

  show(asset) {
    this.visible = true;
    if (this.model) {
      this.model.visible = true;
      el.live2dCanvas.classList.add('live2d-layer-visible');
      el.mosaicLayer.classList.add('live2d-layer-visible');
      el.fallbackTexture.classList.remove('visible');
      this.fit();
      this.renderMosaicOverlay();
      if (!this.hasSceneLayer()) this.startDefaultIdleMotion();
    } else {
      el.fallbackTexture.classList.toggle('visible', this.hasFallbackTexture);
    }
    el.motionBadge.textContent = asset || LIVE2D_TAG;
  }

  async waitLoadComplete(token) {
    const started = performance.now();
    while (token === app.runToken && this.visible && !this.ready && !this.hasFallbackTexture && performance.now() - started < 8000) {
      await waitFrame();
    }
  }

  hide() {
    this.visible = false;
    if (this.model) this.model.visible = false;
    el.live2dCanvas.classList.remove('live2d-layer-visible');
    el.mosaicLayer.classList.remove('live2d-layer-visible');
    el.fallbackTexture.classList.remove('visible');
    el.motionBadge.textContent = 'Live2D hidden';
    this.stopLipSync(true);
    this.clearMosaicOverlay();
  }

  setMosaicMode(mode) {
    this.mosaicMode = normalizeMosaicMode(mode);
    if (this.mosaicMode === 'off') {
      this.mosaicStatus = 'off';
      this.clearMosaicOverlay();
    } else {
      this.renderMosaicOverlay();
    }
  }

  setMosaicEnabled(enabled) {
    this.setMosaicMode(enabled ? 'original' : 'off');
  }

  findMotionForTrigger(name) {
    const text = String(name || '').trim();
    if (!text) return null;
    const exact = this.motionMap.get(normalizeKey(text));
    if (exact?.data) return exact;
    if (isLive2DRuntimeResetName(text)) return null;

    // Story uses logical F/N triggers while exported clips commonly insert the
    // variant letter before the numeric suffix: MouthEmotion06 -> F06,
    // MouthEmotion06_N -> N06.  This convention also appears in single-model
    // stories, so it must not be treated as a multi-character special case.
    const variant = text.match(/^(EyeOpen|EyebrowEmotion|FaceEmotion|MouthEmotion)(\d+)(?:(_N))?$/i);
    if (variant) {
      const family = variant[1];
      const digits = variant[2];
      const letter = variant[3] ? 'N' : 'F';
      for (const candidate of [`${family}${letter}${digits}`, `${family}${digits}`]) {
        const motion = this.motionMap.get(normalizeKey(candidate));
        if (motion?.data) return motion;
      }
    }

    // Some shipped Catalogs contain a real source typo: EyeOpne00..03.
    // Treat it as an alias rather than declaring the Story trigger missing.
    if (/^EyeOpen\d+(?:_N)?$/i.test(text)) {
      const typo = text.replace(/^EyeOpen/i, 'EyeOpne');
      const motion = this.motionMap.get(normalizeKey(typo));
      if (motion?.data) return motion;
    }

    // Animator state aliases do not always equal the AnimationClip filename.
    // Scene07_1 -> scene07_01 / scene07_loop_01; Scene02b_loop -> scene02_loop.
    const sceneVariant = text.match(/^(Scene\d+)_0?1$/i);
    if (sceneVariant) {
      for (const candidate of [`${sceneVariant[1]}_01`, `${sceneVariant[1]}_loop_01`, `${sceneVariant[1]}_loop`]) {
        const motion = this.motionMap.get(normalizeKey(candidate));
        if (motion?.data) return motion;
      }
    }
    const sceneB = text.match(/^(Scene\d+)b_loop$/i);
    if (sceneB) {
      for (const candidate of [`${sceneB[1]}_loop`, sceneB[1]]) {
        const motion = this.motionMap.get(normalizeKey(candidate));
        if (motion?.data) return motion;
      }
    }

    // Scene triggers may reference the persistent loop without the _loop suffix.
    const loop = this.motionMap.get(normalizeKey(`${text}_loop`));
    if (loop?.data) return loop;

    const eye = this.resolveEyeEmotionTrigger(text);
    if (eye?.data) return eye;
    return null;
  }

  resolveEyeEmotionTrigger(name) {
    const text = String(name || '').trim();
    const match = text.match(/^EyeEmotion(TopLeft|TopRight|BottomLeft|BottomRight|Left|Right|Top|Bottom)(?:(_N))?$/i);
    if (!match) return null;
    const direction = match[1].toLowerCase();
    const wantsN = !!match[2];
    const target = {
      left: [1, 0], right: [-1, 0], top: [0, 1], bottom: [0, -1],
      topleft: [0.75, 0.75], topright: [-0.75, 0.75],
      bottomleft: [0.75, -0.75], bottomright: [-0.75, -0.75],
    }[direction];
    if (!target) return null;

    const unique = Array.from(new Set(this.motionMap.values()))
      .filter((motion) => /^EyeEmotion\d+$/i.test(String(motion?.name || fileBase(motion?.path) || '')) && motion?.data);
    const rows = unique.map((motion) => ({ motion, ...this.eyeEmotionVector(motion) })).filter((row) => row.valid);
    if (!rows.length) return null;
    const baseRows = rows.filter((row) => row.variant === 'base');
    const altRows = rows.filter((row) => row.variant !== 'base');
    let candidates = rows;
    if (wantsN && altRows.length) candidates = altRows;
    else if (!wantsN && baseRows.length) candidates = baseRows;
    let best = null;
    let bestScore = Infinity;
    for (const row of candidates) {
      const score = (row.x - target[0]) ** 2 + (row.y - target[1]) ** 2;
      if (score < bestScore) { bestScore = score; best = row.motion; }
    }
    return best;
  }

  eyeEmotionVector(motion) {
    const duration = Math.max(0.001, Number(motion?.data?.Meta?.Duration ?? motion?.duration ?? 0.5));
    let x = null;
    let y = null;
    let xId = '';
    let yId = '';
    for (const curve of motion?.data?.Curves || []) {
      if (curve?.Target !== 'Parameter' || !curve.Id) continue;
      const id = String(curve.Id);
      if (/^ParamEyeBallX\d*$/i.test(id)) {
        const value = evaluateMotionCurve(curve.Segments, duration);
        if (Number.isFinite(Number(value))) { x = Number(value); xId = id; }
      } else if (/^ParamEyeBallY\d*$/i.test(id)) {
        const value = evaluateMotionCurve(curve.Segments, duration);
        if (Number.isFinite(Number(value))) { y = Number(value); yId = id; }
      }
    }
    if (x == null && y == null) return { valid: false, x: 0, y: 0, variant: 'unknown' };
    const numbered = /\d+$/.test(xId.replace(/^ParamEyeBallX/i, '')) || /\d+$/.test(yId.replace(/^ParamEyeBallY/i, ''));
    return { valid: true, x: Number(x || 0), y: Number(y || 0), variant: numbered ? 'alternate' : 'base' };
  }

  motionGroup(motionOrName) {
    if (motionOrName && typeof motionOrName === 'object') {
      return resolveLive2DGroup(motionOrName.group, motionOrName.name || fileBase(motionOrName.path));
    }
    const text = String(motionOrName || '');
    const motion = this.findMotionForTrigger(text);
    return resolveLive2DGroup(motion?.group, motion?.name || stripLive2DResetSuffix(text));
  }

  hasSceneLayer() {
    return Array.from(this.parameterLayers.keys()).some((group) => this.isSceneGroup(group));
  }

  playMotion(name, enabled = true, idle = false, options = {}) {
    const motion = this.findMotionForTrigger(name);
    const replaying = !!options.replaying;
    const replayTarget = !!options.replayTarget;

    if (!enabled) {
      const group = motion ? this.motionGroup(motion) : this.motionGroup(stripLive2DResetSuffix(name));
      if (group) this.fadeMotionGroupToBase(group, replaying ? 0 : null);
      if (!idle) {
        this.currentMotion = '';
        this.updateMotionBadge();
      }
      return;
    }

    if (!idle) this.currentMotion = name || '';

    if (!this.model || !motion?.data) {
      if (this.resetMotionGroup(name, replaying ? 0 : null)) {
        if (!idle) this.currentMotion = '';
        this.updateMotionBadge();
        return;
      }
      if (!idle) {
        el.motionBadge.textContent = name || '';
        pulseFallback();
      }
      return;
    }

    const loop = Boolean(motion.data?.Meta?.Loop || motion.loop);
    this.startParameterMotion(motion, {
      settle: replaying && !replayTarget && !loop,
      restart: !!options.restart,
    });
    this.updateMotionBadge();
  }

  updateMotionBadge() {
    if (!el.motionBadge) return;
    const active = Array.from(this.activeMotionByGroup.entries())
      .sort(([left], [right]) => live2dGroupPriority(left) - live2dGroupPriority(right))
      .map(([group, state]) => `${group}:${state.motion?.name || fileBase(state.motion?.path) || ''}`)
      .filter(Boolean);
    el.motionBadge.textContent = active.length ? active.join(' + ') : (this.currentMotion || '');
  }

  activeMotionSummary() {
    return Array.from(this.activeMotionByGroup.entries())
      .sort(([left], [right]) => live2dGroupPriority(left) - live2dGroupPriority(right))
      .map(([group, state]) => ({
        group,
        motion: state.motion?.name || fileBase(state.motion?.path) || '',
        loop: !!state.loop,
        elapsed: Math.max(0, (performance.now() - Number(state.startedAt || performance.now())) / 1000),
      }));
  }

  isSceneMotionName(name) {
    const motion = this.findMotionForTrigger(name);
    return this.isSceneGroup(this.motionGroup(motion || name));
  }

  startDefaultIdleMotion() {
    if (!this.model) return;
    const idleMotion = this.findDefaultIdleMotion();
    if (!idleMotion?.data) return;
    this.playMotion(idleMotion.name, true, true);
  }

  findDefaultIdleMotion() {
    const motions = Array.from(new Set(this.motionMap.values()));
    return this.motionMap.get(normalizeKey(L2D_DEFAULT_IDLE_MOTION))
      || motions.find((motion) => normalizeKey(motion.group) === 'idle')
      || motions.find((motion) => this.isSceneGroup(this.motionGroup(motion)) && /_loop$/i.test(motion.name || ''))
      || null;
  }

  isSceneGroup(group) {
    return resolveLive2DGroup(group, group) === 'Scene';
  }

  ensureMotionReferenceLayer(group) {
    if (!this.motionLayerReferences.has(group)) this.motionLayerReferences.set(group, new Map());
    return this.motionLayerReferences.get(group);
  }

  motionReferenceValue(group, id) {
    const refs = this.motionLayerReferences.get(group);
    if (refs?.has(id)) return Number(refs.get(id));
    const fallback = this.defaultParameterValue(id);
    return Number.isFinite(Number(fallback)) ? Number(fallback) : 0;
  }

  ensurePartOpacityLayer(group) {
    if (!this.partOpacityLayers.has(group)) this.partOpacityLayers.set(group, new Map());
    return this.partOpacityLayers.get(group);
  }

  ensureModelEffectLayer(group) {
    if (!this.modelEffectLayers.has(group)) this.modelEffectLayers.set(group, new Map());
    return this.modelEffectLayers.get(group);
  }

  startParameterMotion(motion, options = {}) {
    const group = this.motionGroup(motion);
    const loop = Boolean(motion.data?.Meta?.Loop || motion.loop);
    const currentState = this.activeMotionByGroup.get(group);
    if (!options.restart && loop && currentState?.motion === motion && currentState.loop) return;

    const generation = (this.motionGeneration.get(group) || 0) + 1;
    this.motionGeneration.set(group, generation);
    const layer = this.ensureParameterLayer(group);
    const partLayer = this.ensurePartOpacityLayer(group);
    const effectLayer = this.ensureModelEffectLayer(group);

    const duration = Math.max(0.001, Number(motion.data?.Meta?.Duration ?? motion.duration ?? 0.5));
    const previousMotion = this.lastMotionByGroup.get(group);
    const previousValues = new Map(layer);
    const previousPartValues = new Map(partLayer);
    const previousEffectValues = new Map(effectLayer);

    const authoredFadeIn = Math.max(0, Number(motion.fadeInTime ?? motion.data?.Meta?.FadeInTime ?? 0));
    const authoredFadeOut = Math.max(0, Number(previousMotion?.fadeOutTime ?? previousMotion?.data?.Meta?.FadeOutTime ?? 0));
    const hasDirectHandoff = Boolean(previousMotion && previousMotion !== motion && (
      previousValues.size || previousPartValues.size || previousEffectValues.size
    ));
    const HANDOFF_IN = 0.08;
    const HANDOFF_OUT = 0.08;
    const fadeIn = authoredFadeIn > 0 ? authoredFadeIn : (hasDirectHandoff ? HANDOFF_IN : 0);
    const fadeOut = authoredFadeOut > 0 ? authoredFadeOut : (hasDirectHandoff ? HANDOFF_OUT : 0);

    const parameterCurves = (motion.data?.Curves || [])
      .filter((curve) => curve?.Target === 'Parameter' && curve?.Id && Array.isArray(curve.Segments))
      .map((curve) => ({ id: String(curve.Id), curve }));
    const partCurves = (motion.data?.Curves || [])
      .filter((curve) => curve?.Target === 'PartOpacity' && curve?.Id && Array.isArray(curve.Segments))
      .map((curve) => ({ id: String(curve.Id), curve }));
    const modelCurves = (motion.data?.Curves || [])
      .filter((curve) => curve?.Target === 'Model' && curve?.Id && Array.isArray(curve.Segments))
      .map((curve) => ({ id: normalizeLive2DModelCurveId(curve.Id), curve }))
      .filter((entry) => entry.id);

    const parameterMap = new Map(parameterCurves.map((entry) => [entry.id, entry]));
    const partMap = new Map(partCurves.map((entry) => [entry.id, entry]));
    for (const id of partMap.keys()) this.rememberPartOpacityDefault(id);
    const effectMap = new Map(modelCurves.map((entry) => [entry.id, entry]));
    const sceneGroup = this.isSceneGroup(group);
    // MouthEmotion clips are exported as complete mouth states, not additive
    // corrections.  A clip explicitly writes zero to the selectors it wants
    // disabled (ParamMouthFxx / ParamMouthNxx) and sometimes to MouthOpenY.
    // Treating those zeroes as deltas leaves the Scene mouth selectors active
    // underneath, which can combine several mouth meshes and expose dark oral
    // textures / masks.  Keep the old additive semantics for the other facial
    // groups, but make MouthEmotion an absolute override layer.
    const absoluteOverrideGroup = this.isAbsoluteOverrideGroup(group);
    const defaultReferences = new Map();
    if (!sceneGroup && !absoluteOverrideGroup) {
      for (const id of parameterMap.keys()) {
        const reference = this.defaultParameterValue(id);
        defaultReferences.set(id, Number.isFinite(Number(reference)) ? Number(reference) : 0);
      }
    }

    const fadeFrom = new Map();
    for (const id of parameterMap.keys()) {
      let fallback = 0;
      if (sceneGroup) fallback = this.composedSceneBaseValue(id, { skipGroup: group });
      else if (absoluteOverrideGroup) fallback = this.composedParameterValue(id, { skipGroup: group });
      fadeFrom.set(id, previousValues.has(id) ? previousValues.get(id) : fallback);
    }
    const partFadeFrom = new Map();
    for (const id of partMap.keys()) {
      partFadeFrom.set(id, previousPartValues.has(id)
        ? previousPartValues.get(id)
        : this.composedPartOpacity(id, { skipGroup: group }));
    }
    const effectFadeFrom = new Map();
    for (const id of effectMap.keys()) {
      effectFadeFrom.set(id, previousEffectValues.has(id)
        ? previousEffectValues.get(id)
        : live2dModelCurveDefault(id));
    }

    const releaseIds = new Set([...previousValues.keys()].filter((id) => !parameterMap.has(id)));
    const releasePartIds = new Set([...previousPartValues.keys()].filter((id) => !partMap.has(id)));
    const releaseEffectIds = new Set([...previousEffectValues.keys()].filter((id) => !effectMap.has(id)));

    const settle = !!options.settle && !loop;
    const started = performance.now() - (settle ? Math.max(duration, fadeOut) * 1000 : 0);
    this.lastMotionByGroup.set(group, motion);
    this.activeMotionByGroup.set(group, { motion, generation, loop, startedAt: performance.now() });

    const tick = () => {
      if (!this.model || this.motionGeneration.get(group) !== generation) return;

      const elapsed = settle ? Math.max(duration, fadeOut) : (performance.now() - started) / 1000;
      const time = loop ? elapsed % duration : Math.min(elapsed, duration);
      const effectiveFadeIn = Math.min(fadeIn, Math.max(0, duration * 0.35));
      const fadeWeight = settle ? 1 : (effectiveFadeIn > 0 ? smoothStep(clamp(elapsed / effectiveFadeIn, 0, 1)) : 1);
      const releaseWeight = settle ? 1 : (fadeOut > 0 ? smoothStep(clamp(elapsed / fadeOut, 0, 1)) : 1);

      for (const { id, curve } of parameterMap.values()) {
        const value = evaluateMotionCurve(curve.Segments, time);
        if (value == null) continue;
        const authored = Number(value);
        const targetValue = (sceneGroup || absoluteOverrideGroup)
          ? authored
          : authored - (defaultReferences.get(id) ?? 0);
        const from = fadeFrom.get(id);
        // The exported model itself cross-fades mouth selector parameters in
        // Scene motions, and some MouthEmotion clips intentionally enable two
        // selectors at once. Preserve that authored interpolation; the bug was
        // additive composition with Scene, not interpolation inside the clip.
        layer.set(id, from == null ? targetValue : lerp(Number(from), targetValue, fadeWeight));
      }

      for (const id of releaseIds) {
        const from = Number(previousValues.get(id));
        const target = sceneGroup
          ? this.composedSceneBaseValue(id, { skipGroup: group })
          : (absoluteOverrideGroup ? this.composedParameterValue(id, { skipGroup: group }) : 0);
        if (!Number.isFinite(from) || target == null || !Number.isFinite(Number(target))) layer.delete(id);
        else layer.set(id, lerp(from, Number(target), releaseWeight));
        if (releaseWeight >= 1) layer.delete(id);
      }

      for (const { id, curve } of partMap.values()) {
        const value = evaluateMotionCurve(curve.Segments, time);
        if (value == null) continue;
        const from = Number(partFadeFrom.get(id));
        const targetValue = clamp(Number(value), 0, 1);
        partLayer.set(id, Number.isFinite(from) ? lerp(from, targetValue, fadeWeight) : targetValue);
      }
      for (const id of releasePartIds) {
        const from = Number(previousPartValues.get(id));
        const target = this.composedPartOpacity(id, { skipGroup: group });
        partLayer.set(id, lerp(Number.isFinite(from) ? from : target, target, releaseWeight));
        if (releaseWeight >= 1) partLayer.delete(id);
      }

      for (const { id, curve } of effectMap.values()) {
        const value = evaluateMotionCurve(curve.Segments, time);
        if (value == null) continue;
        const from = Number(effectFadeFrom.get(id));
        const targetValue = Number(value);
        effectLayer.set(id, Number.isFinite(from) ? lerp(from, targetValue, fadeWeight) : targetValue);
      }
      for (const id of releaseEffectIds) {
        const from = Number(previousEffectValues.get(id));
        const target = live2dModelCurveDefault(id);
        effectLayer.set(id, lerp(Number.isFinite(from) ? from : target, target, releaseWeight));
        if (releaseWeight >= 1) effectLayer.delete(id);
      }

      this.applyHeldParameters();
      this.applyHeldPartOpacities();
      this.applyHeldModelOpacity();

      const releaseStillRunning = (releaseIds.size || releasePartIds.size || releaseEffectIds.size) && releaseWeight < 1;
      if (loop || elapsed < duration || releaseStillRunning) {
        requestAnimationFrame(tick);
      } else {
        this.startCompanionSceneLoop(motion, generation);
      }
    };

    tick();
  }

  startCompanionSceneLoop(motion, generation) {
    const group = this.motionGroup(motion);
    if (!this.isSceneGroup(group) || this.motionGeneration.get(group) !== generation) return;
    const baseName = motion.name || fileBase(motion.path);
    if (!baseName || /_loop$/i.test(baseName)) return;
    const loopMotion = this.motionMap.get(normalizeKey(`${baseName}_loop`));
    if (loopMotion?.data && loopMotion !== motion) {
      this.startParameterMotion(loopMotion);
      this.updateMotionBadge();
    }
  }

  resetStaleMotionParameters(group, nextMotion) {
    // Layered composition handles stale-parameter release. Never write Core
    // directly here, or a handoff can briefly overwrite the composed result.
    return;
  }

  parameterIdsForMotion(motion) {
    const ids = new Set();
    for (const curve of motion?.data?.Curves || []) {
      if (curve?.Target === 'Parameter' && curve.Id) ids.add(curve.Id);
    }
    return ids;
  }
  ensureParameterLayer(group) {
    if (!this.parameterLayers.has(group)) this.parameterLayers.set(group, new Map());
    return this.parameterLayers.get(group);
  }

  ensureMotionQueue(group) {
    if (!this.motionQueues.has(group)) {
      const QueueManager = window.PIXI?.live2d?.CubismMotionQueueManager;
      if (!QueueManager) return null;
      this.motionQueues.set(group, new QueueManager());
    }
    return this.motionQueues.get(group);
  }

  createSdkMotion(motion, group, onFinished) {
    const CubismMotion = window.PIXI?.live2d?.CubismMotion;
    if (!CubismMotion || !motion?.data) return null;
    const sdkMotion = CubismMotion.create(this.sdkCompatibleMotionData(motion.data), onFinished);
    const loop = Boolean(motion.data?.Meta?.Loop || motion.loop);
    sdkMotion.setIsLoop?.(loop);
    sdkMotion.setIsLoopFadeIn?.(false);
    const fadeIn = Number(motion.fadeInTime ?? motion.FadeInTime);
    const fadeOut = Number(motion.fadeOutTime ?? motion.FadeOutTime);
    if (Number.isFinite(fadeIn) && fadeIn >= 0) sdkMotion.setFadeInTime?.(fadeIn);
    if (Number.isFinite(fadeOut) && fadeOut >= 0) sdkMotion.setFadeOutTime?.(fadeOut);
    const manager = this.model?.internalModel?.motionManager;
    sdkMotion.setEffectIds?.(manager?.eyeBlinkIds || [], manager?.lipSyncIds || []);
    return sdkMotion;
  }

  sdkCompatibleMotionData(data) {
    if (!data?.Curves?.length) return data;
    return {
      ...data,
      Curves: data.Curves.map((curve) => {
        const segments = curve?.Segments;
        if (!Array.isArray(segments) || segments.length < 6) return curve;
        const hasUnityLinearTail = segments[segments.length - 1] === 0 && (segments.length - 2) % 3 === 1;
        return hasUnityLinearTail ? { ...curve, Segments: segments.slice(0, -1) } : curve;
      }),
    };
  }

  updateMotionQueues() {
    const core = this.model?.internalModel?.coreModel;
    if (!core) return;
    const now = this.motionClockSeconds();
    const groups = Array.from(this.motionQueues.keys()).sort((left, right) => {
      const diff = live2dGroupPriority(left) - live2dGroupPriority(right);
      return diff || String(left).localeCompare(String(right));
    });
    for (const group of groups) {
      const queue = this.motionQueues.get(group);
      if (!queue) continue;
      queue.doUpdateMotion(core, now);
      if (queue.isFinished?.()) this.motionQueues.delete(group);
    }
  }

  motionClockSeconds() {
    return performance.now() / 1000;
  }

  ensureParameterHoldLoop() {
    if (this.holdFrame) cancelAnimationFrame(this.holdFrame);
    const tick = () => {
      if (!this.model) {
        this.holdFrame = 0;
        return;
      }
      this.updateLipSync();
      this.applyHeldParameters();
      this.applyHeldPartOpacities();
      this.applyHeldModelOpacity();
      this.renderMosaicOverlay();
      this.holdFrame = requestAnimationFrame(tick);
    };
    this.holdFrame = requestAnimationFrame(tick);
  }

  installLive2DUpdateHook() {
    const internal = this.model?.internalModel;
    if (!internal?.on) return;
    this.uninstallLive2DUpdateHook();
    this.live2dMotionUpdateHandler = () => {
      this.updateMotionQueues();
    };
    this.live2dUpdateHandler = () => {
      this.updateLipSync();
      this.applyHeldParameters();
      this.applyHeldPartOpacities();
      this.applyHeldModelOpacity();
    };
    internal.on('afterMotionUpdate', this.live2dMotionUpdateHandler);
    internal.on('beforeModelUpdate', this.live2dUpdateHandler);
    this.live2dUpdateHookInstalled = true;
  }

  uninstallLive2DUpdateHook() {
    if (this.live2dMotionUpdateHandler) {
      const internal = this.model?.internalModel;
      try { internal?.off?.('afterMotionUpdate', this.live2dMotionUpdateHandler); } catch (_) {}
      try { internal?.removeListener?.('afterMotionUpdate', this.live2dMotionUpdateHandler); } catch (_) {}
    }
    if (this.live2dUpdateHandler) {
      const internal = this.model?.internalModel;
      try { internal?.off?.('beforeModelUpdate', this.live2dUpdateHandler); } catch (_) {}
      try { internal?.removeListener?.('beforeModelUpdate', this.live2dUpdateHandler); } catch (_) {}
    }
    this.live2dMotionUpdateHandler = null;
    this.live2dUpdateHandler = null;
    this.live2dUpdateHookInstalled = false;
  }

  cacheMosaicDrawables() {
    const internal = this.model?.internalModel;
    const ids = internal?.getDrawableIDs?.()
      || internal?.coreModel?.getDrawableIds?.()
      || internal?.coreModel?._model?.drawables?.ids
      || [];
    this.mosaicDrawables = Array.from(ids)
      .map((id, index) => ({ id: String(id), index }))
      .filter((item) => item.id.startsWith('Mosaic_') || item.id.startsWith('MosaicInsted_'))
      .map((item) => ({
        ...item,
        material: item.id.startsWith('MosaicInsted_') ? 'inverted' : 'mosaic',
      }));
    const mosaicCount = this.mosaicDrawables.filter((item) => item.material === 'mosaic').length;
    const invertedCount = this.mosaicDrawables.filter((item) => item.material === 'inverted').length;
    this.mosaicStatus = this.mosaicDrawables.length
      ? `${mosaicCount} Mosaic_ / ${invertedCount} MosaicInsted_ masks`
      : 'no mosaic drawables';
  }

  installMosaicDrawableSuppression() {
    const internal = this.model?.internalModel;
    if (!internal || this.mosaicDrawPatchInstalled || typeof internal.draw !== 'function') return;
    const originalDraw = internal.draw.bind(internal);
    internal.draw = (gl) => {
      this.suppressMosaicDrawables();
      return originalDraw(gl);
    };
    this.mosaicDrawPatchInstalled = true;
  }

  suppressMosaicDrawables() {
    const opacities = this.model?.internalModel?.coreModel?._model?.drawables?.opacities;
    if (!opacities || !this.mosaicDrawables.length) return;
    for (const drawable of this.mosaicDrawables) {
      if (drawable.index >= 0 && drawable.index < opacities.length) opacities[drawable.index] = 0;
    }
  }

  renderMosaicOverlay() {
    const canvas = el.mosaicLayer;
    if (!canvas) return;
    const drawables = this.activeMosaicDrawables();
    if (this.mosaicMode === 'off' || !this.visible || !this.model || !drawables.length) {
      this.clearMosaicOverlay();
      return;
    }

    const dpr = resizeOverlayCanvas(canvas);
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const source = el.live2dCanvas;
    if (!source?.width || !source?.height) return;
    const block = Math.max(2, Math.floor(Math.min(canvas.width / MOSAIC_PIXEL_DIV_X, canvas.height / MOSAIC_PIXEL_DIV_Y)));
    let drew = false;
    for (const drawable of drawables) {
      const mesh = this.drawableStageMesh(drawable.index, dpr);
      if (!mesh || mesh.rect.width <= 1 || mesh.rect.height <= 1) continue;
      pixelateCanvasMesh(source, canvas, this.mosaicScratch, mesh, block);
      drew = true;
    }
    this.mosaicStatus = drew
      ? `${drawables.length} ${mosaicModeLabel(this.mosaicMode)} masks`
      : 'drawable bounds unavailable';
  }

  activeMosaicDrawables() {
    if (this.mosaicMode === 'off') return [];
    if (this.mosaicMode === 'mosaic') return this.mosaicDrawables.filter((item) => item.material === 'mosaic');
    if (this.mosaicMode === 'inverted') return this.mosaicDrawables.filter((item) => item.material === 'inverted');
    return this.mosaicDrawables;
  }

  drawableStageMesh(index, dpr) {
    if (!window.PIXI?.Point || !this.model?.toGlobal) return null;
    const vertices = this.drawableVertices(index);
    const indices = this.drawableVertexIndices(index);
    if (!vertices?.length || !indices?.length) return null;

    const points = [];
    const canvas = el.mosaicLayer;
    const maxWidth = canvas?.width || 1;
    const maxHeight = canvas?.height || 1;
    let left = Infinity;
    let right = -Infinity;
    let top = Infinity;
    let bottom = -Infinity;
    for (let i = 0; i + 1 < vertices.length; i += 2) {
      const point = this.model.toGlobal(new PIXI.Point(Number(vertices[i]) || 0, Number(vertices[i + 1]) || 0));
      const x = point.x * dpr;
      const y = point.y * dpr;
      points.push({ x, y });
      left = Math.min(left, x);
      right = Math.max(right, x);
      top = Math.min(top, y);
      bottom = Math.max(bottom, y);
    }
    if (!Number.isFinite(left) || !Number.isFinite(top)) return null;

    left = clamp(Math.floor(left) - MOSAIC_PADDING_PX * dpr, 0, maxWidth);
    top = clamp(Math.floor(top) - MOSAIC_PADDING_PX * dpr, 0, maxHeight);
    right = clamp(Math.ceil(right) + MOSAIC_PADDING_PX * dpr, 0, maxWidth);
    bottom = clamp(Math.ceil(bottom) + MOSAIC_PADDING_PX * dpr, 0, maxHeight);
    return {
      points,
      indices: Array.from(indices),
      rect: {
        x: left,
        y: top,
        width: Math.max(0, right - left),
        height: Math.max(0, bottom - top),
      },
    };
  }

  drawableVertices(index) {
    const internal = this.model?.internalModel;
    try {
      const vertices = internal?.getDrawableVertices?.(index);
      if (vertices?.length) return Array.from(vertices);
    } catch (_) {}
    try {
      const vertices = internal?.coreModel?.getDrawableVertices?.(index);
      if (vertices?.length) return Array.from(vertices);
    } catch (_) {}
    return null;
  }

  drawableVertexIndices(index) {
    const core = this.model?.internalModel?.coreModel;
    try {
      const indices = core?.getDrawableVertexIndices?.(index);
      if (indices?.length) return Array.from(indices);
    } catch (_) {}
    try {
      const indices = core?._model?.drawables?.indices?.[index];
      if (indices?.length) return Array.from(indices);
    } catch (_) {}
    return null;
  }

  drawableBounds(index) {
    const internal = this.model?.internalModel;
    if (!internal) return null;
    try {
      if (typeof internal.getDrawableBounds === 'function') {
        const bounds = internal.getDrawableBounds(index, {});
        if (bounds && Number.isFinite(bounds.width) && Number.isFinite(bounds.height)) return bounds;
      }
    } catch (_) {}
    try {
      const vertices = internal.getDrawableVertices?.(index);
      if (!vertices?.length) return null;
      let left = Infinity;
      let right = -Infinity;
      let top = Infinity;
      let bottom = -Infinity;
      for (let i = 0; i + 1 < vertices.length; i += 2) {
        const x = Number(vertices[i]);
        const y = Number(vertices[i + 1]);
        if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
        left = Math.min(left, x);
        right = Math.max(right, x);
        top = Math.min(top, y);
        bottom = Math.max(bottom, y);
      }
      if (!Number.isFinite(left) || !Number.isFinite(top)) return null;
      return { x: left, y: top, width: right - left, height: bottom - top };
    } catch (_) {
      return null;
    }
  }

  drawableStageRect(bounds, dpr) {
    if (!window.PIXI?.Point || !this.model?.toGlobal) return null;
    const p1 = this.model.toGlobal(new PIXI.Point(bounds.x, bounds.y));
    const p2 = this.model.toGlobal(new PIXI.Point(bounds.x + bounds.width, bounds.y + bounds.height));
    const left = Math.min(p1.x, p2.x) - MOSAIC_PADDING_PX;
    const top = Math.min(p1.y, p2.y) - MOSAIC_PADDING_PX;
    const right = Math.max(p1.x, p2.x) + MOSAIC_PADDING_PX;
    const bottom = Math.max(p1.y, p2.y) + MOSAIC_PADDING_PX;
    const maxWidth = el.stage.clientWidth || 1;
    const maxHeight = el.stage.clientHeight || 1;
    const clippedLeft = clamp(left, 0, maxWidth);
    const clippedTop = clamp(top, 0, maxHeight);
    const clippedRight = clamp(right, 0, maxWidth);
    const clippedBottom = clamp(bottom, 0, maxHeight);
    return {
      x: Math.round(clippedLeft * dpr),
      y: Math.round(clippedTop * dpr),
      width: Math.round((clippedRight - clippedLeft) * dpr),
      height: Math.round((clippedBottom - clippedTop) * dpr),
    };
  }

  clearMosaicOverlay() {
    const canvas = el.mosaicLayer;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (ctx) ctx.clearRect(0, 0, canvas.width || 1, canvas.height || 1);
  }

  applyHeldParameters() {
    this.heldParameters.clear();
    const groups = this.sortedMotionGroups(this.parameterLayers.keys());

    // Scene/Idle is the absolute base. Other animation groups are authored as
    // offsets from their parameter defaults, so several independent groups can
    // play at the same time without flattening the underlying body motion.
    for (const group of groups) {
      if (!this.isSceneGroup(group)) continue;
      const layer = this.parameterLayers.get(group);
      if (!layer) continue;
      for (const [id, value] of layer.entries()) {
        const n = Number(value);
        if (Number.isFinite(n)) this.heldParameters.set(id, n);
      }
    }

    for (const group of groups) {
      if (this.isSceneGroup(group) || group === L2D_LIP_SYNC.layer) continue;
      const layer = this.parameterLayers.get(group);
      if (!layer) continue;
      const absoluteOverrideGroup = this.isAbsoluteOverrideGroup(group);
      for (const [id, value] of layer.entries()) {
        const n = Number(value);
        if (!Number.isFinite(n)) continue;
        if (absoluteOverrideGroup) {
          this.heldParameters.set(id, n);
          continue;
        }
        const base = this.heldParameters.has(id)
          ? Number(this.heldParameters.get(id))
          : Number(this.defaultParameterValue(id));
        if (Number.isFinite(base)) this.heldParameters.set(id, base + n);
      }
    }

    // motion3 Model curves are evaluated after Parameter curves, matching the
    // Cubism runtime: EyeBlink multiplies eye-open parameters and LipSync adds
    // to mouth-open parameters. A parameter curve in the same group wins over
    // the group's Model effect for that parameter.
    const effectGroups = this.sortedMotionGroups(this.modelEffectLayers.keys());
    const voiceLip = this.lipSync;
    const voiceLipActive = !!(voiceLip?.useMultiCharacterLipSync && voiceLip.parameterId && Number.isFinite(Number(voiceLip.mouseY)));
    const voiceLipTargetId = voiceLipActive ? String(voiceLip.parameterId) : '';

    for (const group of effectGroups) {
      const effects = this.modelEffectLayers.get(group);
      if (!effects) continue;
      const parameterLayer = this.parameterLayers.get(group);

      if (effects.has('eyeBlink')) {
        const factor = clamp(Number(effects.get('eyeBlink')), 0, 1);
        if (Number.isFinite(factor)) {
          for (const id of this.effectParameterIds('eyeBlink')) {
            if (parameterLayer?.has(id)) continue;
            const base = this.heldParameters.has(id)
              ? Number(this.heldParameters.get(id))
              : Number(this.defaultParameterValue(id));
            if (Number.isFinite(base)) this.heldParameters.set(id, base * factor);
          }
        }
      }

      if (effects.has('lipSync') && !voiceLipActive) {
        const addition = Number(effects.get('lipSync'));
        if (Number.isFinite(addition)) {
          for (const id of this.effectParameterIds('lipSync')) {
            if (parameterLayer?.has(id)) continue;
            const base = this.heldParameters.has(id)
              ? Number(this.heldParameters.get(id))
              : Number(this.defaultParameterValue(id));
            if (Number.isFinite(base)) this.heldParameters.set(id, base + addition);
          }
        }
      }
    }

    // Voice LipSync stays above scripted mouth motion, but it now uses the
    // remaining mouth-open headroom instead of blindly adding 0..1. This keeps
    // MouthEmotion (smile/grimace/closed-mouth poses) visible and avoids the
    // frequent hard clamp at ParamMouthOpenY max.
    if (voiceLipActive) {
      const base = this.heldParameters.has(voiceLipTargetId)
        ? Number(this.heldParameters.get(voiceLipTargetId))
        : Number(this.defaultParameterValue(voiceLipTargetId));
      const opening = Number(voiceLip.mouseY);
      if (Number.isFinite(base) && Number.isFinite(opening)) {
        this.heldParameters.set(
          voiceLipTargetId,
          this.composeVoiceLipSyncValue(voiceLipTargetId, base, opening),
        );
      }
      const formId = String(voiceLip.formParameterId || '');
      if (formId && this.actualParameterExists(formId)) {
        const formBase = this.heldParameters.has(formId)
          ? Number(this.heldParameters.get(formId))
          : Number(this.defaultParameterValue(formId));
        if (Number.isFinite(formBase)) {
          this.heldParameters.set(formId, this.composeVoiceLipFormValue(formId, formBase, voiceLip.formY));
        }
      }
    }

    // Parameters that were controlled on the previous frame but are no longer
    // owned by any live layer must be restored. Without this, a completed/reset
    // action can leave a stale pose stuck in the Core model indefinitely.
    for (const id of this.appliedParameterIds) {
      if (this.heldParameters.has(id)) continue;
      const fallback = this.defaultParameterValue(id);
      if (fallback != null && Number.isFinite(Number(fallback))) this.setParameter(id, Number(fallback));
    }

    const nextApplied = new Set();
    for (const [id, value] of this.heldParameters.entries()) {
      this.setParameter(id, value);
      nextApplied.add(id);
    }
    this.appliedParameterIds = nextApplied;
  }

  sortedMotionGroups(groups) {
    return Array.from(groups).sort((left, right) => {
      const diff = live2dGroupPriority(left) - live2dGroupPriority(right);
      return diff || String(left).localeCompare(String(right));
    });
  }

  effectParameterIds(kind) {
    const manager = this.model?.internalModel?.motionManager;
    const raw = kind === 'eyeBlink' ? manager?.eyeBlinkIds : manager?.lipSyncIds;
    const fromManager = live2dIdArray(raw).filter((id) => this.actualParameterExists(id));
    if (fromManager.length) return fromManager;
    const ids = this.actualParameterIds();
    if (kind === 'eyeBlink') return ids.filter((id) => /ParamEye.*Open/i.test(id));
    return ids.filter((id) => /^ParamMouthOpenY\d*$/i.test(id));
  }

  composedPartOpacity(id, options = {}) {
    const skipGroup = options.skipGroup || '';
    let value = this.defaultPartOpacityValue(id);
    for (const group of this.sortedMotionGroups(this.partOpacityLayers.keys())) {
      if (group === skipGroup) continue;
      const layer = this.partOpacityLayers.get(group);
      if (layer?.has(id)) value = clamp(Number(layer.get(id)), 0, 1);
    }
    return Number.isFinite(value) ? value : 1;
  }

  rememberPartOpacityDefault(id) {
    if (!id || this.partOpacityDefaults.has(id)) return;
    const current = this.currentPartOpacity(id);
    this.partOpacityDefaults.set(id, Number.isFinite(Number(current)) ? Number(current) : 1);
  }

  defaultPartOpacityValue(id) {
    if (this.partOpacityDefaults.has(id)) return Number(this.partOpacityDefaults.get(id));
    this.rememberPartOpacityDefault(id);
    return this.partOpacityDefaults.has(id) ? Number(this.partOpacityDefaults.get(id)) : 1;
  }

  currentPartOpacity(id) {
    const core = this.model?.internalModel?.coreModel;
    if (!core || !id) return 1;
    try {
      if (typeof core.getPartOpacityById === 'function') return core.getPartOpacityById(id);
    } catch (_) {}
    try {
      const model = core._model || core.getModel?.();
      const ids = model?.parts?.ids || core._partIds || [];
      const opacities = model?.parts?.opacities;
      const index = Array.from(ids, live2dIdString).indexOf(String(id));
      if (opacities && index >= 0 && index < opacities.length) return Number(opacities[index]);
    } catch (_) {}
    return 1;
  }

  applyHeldPartOpacities() {
    const composed = new Map();
    for (const group of this.sortedMotionGroups(this.partOpacityLayers.keys())) {
      const layer = this.partOpacityLayers.get(group);
      if (!layer) continue;
      for (const [id, value] of layer.entries()) {
        const n = Number(value);
        if (Number.isFinite(n)) composed.set(id, clamp(n, 0, 1));
      }
    }
    for (const id of this.appliedPartOpacityIds) {
      if (!composed.has(id)) this.setPartOpacity(id, this.defaultPartOpacityValue(id));
    }
    for (const [id, value] of composed.entries()) this.setPartOpacity(id, value);
    this.appliedPartOpacityIds = new Set(composed.keys());
  }

  setPartOpacity(id, value) {
    const core = this.model?.internalModel?.coreModel;
    const opacity = clamp(Number(value), 0, 1);
    if (!core || !Number.isFinite(opacity)) return;
    try {
      if (typeof core.setPartOpacityById === 'function') {
        core.setPartOpacityById(id, opacity);
        return;
      }
    } catch (_) {}
    try {
      const model = core._model || core.getModel?.();
      const ids = model?.parts?.ids || core._partIds || [];
      const opacities = model?.parts?.opacities;
      const index = Array.from(ids, live2dIdString).indexOf(String(id));
      if (opacities && index >= 0 && index < opacities.length) opacities[index] = opacity;
    } catch (_) {}
  }

  applyHeldModelOpacity() {
    if (!this.model) return;
    let opacity = 1;
    for (const group of this.sortedMotionGroups(this.modelEffectLayers.keys())) {
      const layer = this.modelEffectLayers.get(group);
      if (layer?.has('opacity')) opacity = clamp(Number(layer.get('opacity')), 0, 1);
    }
    if (!Number.isFinite(opacity)) opacity = 1;
    try {
      const core = this.model.internalModel?.coreModel;
      if (typeof core?.setModelOpacity === 'function') core.setModelOpacity(opacity);
      else this.model.alpha = opacity;
    } catch (_) {
      this.model.alpha = opacity;
    }
  }

  setParameter(id, value) {
    try {
      const core = this.model?.internalModel?.coreModel;
      if (!core || value == null || !Number.isFinite(Number(value))) return;
      core.setParameterValueById?.(id, this.clampParameterValue(id, Number(value)));
    } catch (_) {}
  }

  clampParameterValue(id, value) {
    const core = this.model?.internalModel?.coreModel;
    if (!core) return value;
    try {
      const index = core.getParameterIndex?.(id);
      if (!Number.isInteger(index) || index < 0) return value;
      const min = core.getParameterMinimumValue?.(index);
      const max = core.getParameterMaximumValue?.(index);
      if (Number.isFinite(min) && Number.isFinite(max)) return clamp(value, min, max);
    } catch (_) {}
    return value;
  }

  isMouthEmotionGroup(group) {
    return normalizeKey(group).startsWith('mouthemotion');
  }

  isAbsoluteOverrideGroup(group) {
    // Keep the previously preferred additive composition for face/eye layers,
    // but MouthEmotion is authored as a complete state and must replace the
    // Scene values for every parameter it explicitly controls.
    return this.isMouthEmotionGroup(group);
  }

  isMouthParameterId(id) {
    const text = String(id || '');
    return /^ParamMouth/i.test(text)
      || /^Paramtongue$/i.test(text)
      || this.mouthParameterIds.has(text);
  }

  isDiscreteMouthShapeParameter(id) {
    const text = String(id || '');
    return /^ParamMouth(?:F|N)\d+$/i.test(text)
      || /^ParamMouthShape\d+$/i.test(text)
      || /^ParamMouthType\d+$/i.test(text);
  }

  hasParameter(id) {
    return this.actualParameterExists(id);
  }

  actualParameterIds() {
    const core = this.model?.internalModel?.coreModel;
    if (!core) return [];
    const candidates = [
      core._parameterIds,
      core.parameterIds,
      core.parameters?.ids,
      core._parameterIds?.values,
      core._model?.parameters?.ids,
      core.getModel?.()?.parameters?.ids,
    ];
    for (const source of candidates) {
      if (!source) continue;
      if (Array.isArray(source)) return source.map(live2dIdString).filter(Boolean);
      if (source instanceof Set) return Array.from(source, live2dIdString).filter(Boolean);
      if (typeof source.length === 'number') {
        try { return Array.from(source, live2dIdString).filter(Boolean); } catch (_) {}
      }
    }
    return [];
  }

  actualParameterExists(id) {
    const core = this.model?.internalModel?.coreModel;
    if (!core || !id) return false;
    const ids = this.actualParameterIds();
    if (ids.length) return ids.includes(id);
    return this.mouthParameterIds.has(String(id));
  }

  resolveLipSyncParameterId(suffix, mode = 'multi') {
    const clean = String(suffix || '').trim();
    const key = normalizeKey(mode || 'multi');
    if (key === 'off' || key === 'none' || key === 'false' || key === '0') return '';
    // Single/default mode uses the ordinary Cubism mouth parameter when the
    // model exports one. Multi mode keeps the character-suffixed parameters.
    if (key === 'single' || key === 'normal' || key === 'default') {
      if (this.actualParameterExists(L2D_LIP_SYNC.mouthParameterPrefix)) return L2D_LIP_SYNC.mouthParameterPrefix;
      return clean ? this.resolveExportedLipSyncParameterId(clean) : '';
    }
    if (!clean) return '';
    const parameterId = `${L2D_LIP_SYNC.mouthParameterPrefix}${clean}`;
    if (this.actualParameterExists(parameterId)) return parameterId;
    return this.resolveExportedLipSyncParameterId(clean);
  }

  resolveExportedLipSyncParameterId(suffix) {
    const numeric = Number.parseInt(suffix, 10);
    if (!Number.isFinite(numeric) || numeric <= 0) return '';
    const compactId = `${L2D_LIP_SYNC.mouthParameterPrefix}${numeric}`;
    if (this.actualParameterExists(compactId)) return compactId;
    if (this.actualParameterExists(L2D_LIP_SYNC.mouthParameterPrefix)) {
      return L2D_LIP_SYNC.mouthParameterPrefix;
    }
    return '';
  }

  resolveLipSyncFormParameterId(openParameterId, suffix = '') {
    // Only continuous mouth-form parameters are eligible. The game's
    // ParamMouthFxx / ParamMouthNxx values select authored mouth meshes and
    // must remain exclusively controlled by MouthEmotion/Scene motions.
    const openId = String(openParameterId || '');
    const cleanSuffix = String(suffix || '').trim();
    const candidates = [];
    if (openId === L2D_LIP_SYNC.mouthParameterPrefix) candidates.push('ParamMouthForm');
    if (cleanSuffix) candidates.push(`ParamMouthForm${cleanSuffix}`);
    if (!cleanSuffix || cleanSuffix === '1') candidates.push('ParamMouthForm');
    for (const id of candidates) {
      if (!/^ParamMouthForm\d*$/i.test(id)) continue;
      if (this.actualParameterExists(id)) return id;
    }
    return '';
  }

  composeVoiceLipFormValue(id, base, formAmount) {
    const runtime = this.parameterRange(id);
    if (!runtime) return this.clampParameterValue(id, Number(base));
    const authored = this.authoredParameterRange(id);
    const min = authored ? Math.max(runtime.min, Number(authored.min)) : runtime.min;
    const max = authored ? Math.min(runtime.max, Number(authored.max)) : runtime.max;
    if (!(max > min)) return this.clampParameterValue(id, Number(base));
    const center = clamp(Number(base), min, max);
    const signed = clamp(Number(formAmount) || 0, -1, 1);
    const room = signed >= 0 ? Math.max(0, max - center) : Math.max(0, center - min);
    const influence = clamp(Number(this.lipSync?.weight ?? 1), 0, 1);
    return clamp(center + signed * room * L2D_LIP_SYNC.formDrive * influence, min, max);
  }

  composedSceneBaseValue(id, options = {}) {
    const skipGroup = options.skipGroup || '';
    let value = this.defaultParameterValue(id);
    if (!Number.isFinite(Number(value))) value = 0;
    for (const group of this.sortedMotionGroups(this.parameterLayers.keys())) {
      if (group === skipGroup || !this.isSceneGroup(group)) continue;
      const layer = this.parameterLayers.get(group);
      if (layer?.has(id)) value = Number(layer.get(id));
    }
    return this.clampParameterValue(id, value);
  }

  composedParameterValue(id, options = {}) {
    const skipGroup = options.skipGroup || '';
    const groups = this.sortedMotionGroups(this.parameterLayers.keys());

    let value = this.defaultParameterValue(id);
    if (!Number.isFinite(Number(value))) value = 0;

    for (const group of groups) {
      if (group === skipGroup || !this.isSceneGroup(group)) continue;
      const layer = this.parameterLayers.get(group);
      if (layer?.has(id)) value = Number(layer.get(id));
    }

    // Most non-Scene layers remain additive, matching the behavior that works
    // best for the exported face/eye/body corrections. MouthEmotion is the
    // exception: its curves describe a complete mouth state and must override
    // the Scene value for the parameters the clip explicitly owns.
    for (const group of groups) {
      if (group === skipGroup || this.isSceneGroup(group) || group === L2D_LIP_SYNC.layer) continue;
      const layer = this.parameterLayers.get(group);
      if (!layer?.has(id)) continue;
      const n = Number(layer.get(id));
      if (!Number.isFinite(n)) continue;
      if (this.isAbsoluteOverrideGroup(group)) value = n;
      else value += n;
    }

    return this.clampParameterValue(id, value);
  }

  lipSyncBaseParameterValue(id) {
    return this.composedParameterValue(id, { skipGroup: L2D_LIP_SYNC.layer });
  }

  currentMouthShapeEntries() {
    const entries = [];
    for (const [group, layer] of this.parameterLayers.entries()) {
      if (group === L2D_LIP_SYNC.layer || !layer) continue;
      const isMouthGroup = normalizeKey(group).startsWith('mouthemotion');
      for (const [id, value] of layer.entries()) {
        const isNamedMouthShape = /^ParamMouthForm/i.test(id) || /^Paramtongue$/i.test(id);
        const isMouthGroupShape = isMouthGroup && this.isDiscreteMouthShapeParameter(id);
        if (isNamedMouthShape || isMouthGroupShape) entries.push([id, Number(value) || 0]);
      }
    }
    return entries;
  }

  currentMouthFormIntensity() {
    return this.currentMouthShapeEntries()
      .reduce((intensity, [, value]) => Math.max(intensity, Math.abs(value)), 0);
  }

  parameterRange(id) {
    const core = this.model?.internalModel?.coreModel;
    if (!core || !id) return null;
    try {
      const index = core.getParameterIndex?.(id);
      if (!Number.isInteger(index) || index < 0) return null;
      const min = Number(core.getParameterMinimumValue?.(index));
      const max = Number(core.getParameterMaximumValue?.(index));
      if (Number.isFinite(min) && Number.isFinite(max) && max > min) return { min, max };
    } catch (_) {}
    return null;
  }

  authoredParameterRange(id) {
    const key = String(id || '');
    if (!key) return null;
    if (this.authoredParameterRanges.has(key)) return this.authoredParameterRanges.get(key);
    let min = Infinity;
    let max = -Infinity;
    for (const motion of new Set(this.motionMap.values())) {
      const duration = Math.max(0.001, Number(motion?.data?.Meta?.Duration ?? motion?.duration ?? 0.5));
      for (const curve of motion?.data?.Curves || []) {
        if (curve?.Target !== 'Parameter' || String(curve.Id) !== key) continue;
        // Sampling the evaluated curve avoids misreading segment type/time
        // numbers as parameter values. 24 intervals are enough for authored
        // mouth curves while keeping the first-use cost tiny.
        for (let step = 0; step <= 24; step += 1) {
          const value = evaluateMotionCurve(curve.Segments, duration * step / 24);
          if (!Number.isFinite(Number(value))) continue;
          min = Math.min(min, Number(value));
          max = Math.max(max, Number(value));
        }
      }
    }
    const range = Number.isFinite(min) && Number.isFinite(max) && max >= min ? { min, max } : null;
    this.authoredParameterRanges.set(key, range);
    return range;
  }

  mouthTransitionGain() {
    const state = this.activeMotionByGroup.get('MouthEmotion');
    if (!state?.startedAt) return 1;
    const age = Math.max(0, (performance.now() - Number(state.startedAt)) / 1000);
    return smoothStep(clamp(age / Math.max(0.001, L2D_LIP_SYNC.mouthTransitionGuardSec), 0, 1));
  }

  composeVoiceLipSyncValue(id, base, opening) {
    const amount = clamp(Number(opening) || 0, 0, 1);
    const runtime = this.parameterRange(id);
    if (!runtime) return this.clampParameterValue(id, Number(base));

    const authored = this.authoredParameterRange(id);
    let safeMin = runtime.min;
    let safeMax = runtime.max;
    if (authored) {
      safeMin = Math.max(runtime.min, Number(authored.min));
      safeMax = Math.min(runtime.max, Number(authored.max));
    }
    if (!(safeMax > safeMin)) {
      safeMin = runtime.min;
      safeMax = runtime.min + (runtime.max - runtime.min) * 0.72;
    }

    // Never let speech push the mesh outside the range that the model's own
    // authored motions actually use. The primary figure-eight/black-block bug
    // is fixed in MouthEmotion composition above; this clamp is a second safety
    // boundary so voice drive cannot create a parameter combination the model
    // author never used.
    const authoredNeutral = clamp(Number(base), safeMin, safeMax);
    const shapeIntensity = clamp(this.currentMouthFormIntensity(), 0, 1);
    const drive = lerp(L2D_LIP_SYNC.safeAuthoredDrive, L2D_LIP_SYNC.strongShapeDrive, shapeIntensity);
    const transitionGain = this.mouthTransitionGain();
    // During speech, allow the jaw to close slightly below the scene's static
    // mouth-open value. Otherwise a scene authored at OpenY=0.4 can only move
    // 0.4 -> 1.0 and looks like a hinged mouth that never forms syllables.
    const closed = lerp(authoredNeutral, safeMin, L2D_LIP_SYNC.speechClosureBlend);
    const top = authoredNeutral + Math.max(0, safeMax - authoredNeutral) * drive;
    const speechValue = clamp(lerp(closed, top, amount * transitionGain), safeMin, safeMax);
    const influence = clamp(Number(this.lipSync?.weight ?? 1), 0, 1);
    return clamp(lerp(authoredNeutral, speechValue, influence), safeMin, safeMax);
  }

  disableSdkIdleMotion() {
    const motionManager = this.model?.internalModel?.motionManager;
    if (!motionManager) return;
    try { motionManager.stopAllMotions?.(); } catch (_) {}
    try { motionManager._stopAllMotions?.(); } catch (_) {}
    if (motionManager.groups) {
      delete motionManager.groups.idle;
      delete motionManager.groups.Idle;
    }
    if (motionManager.definitions) {
      motionManager.definitions.Idle = [];
      motionManager.definitions.idle = [];
    }
    if (motionManager.motionGroups) {
      motionManager.motionGroups.Idle = [];
      motionManager.motionGroups.idle = [];
    }
    if (motionManager.state) {
      motionManager.state.currentGroup = '';
      motionManager.state.currentIndex = -1;
      motionManager.state.currentPriority = 0;
      motionManager.state.reservePriority = 0;
    }
    motionManager.playing = false;
  }

  disableSdkNaturalMotion() {
    const internalModel = this.model?.internalModel;
    if (!internalModel) return;
    try { internalModel.updateNaturalMovements = () => {}; } catch (_) {}
    try { internalModel.updateFocus = () => {}; } catch (_) {}
    try { internalModel.breath = null; } catch (_) {}
    try {
      if (internalModel.focusController) {
        internalModel.focusController.update = () => {};
        internalModel.focusController.x = 0;
        internalModel.focusController.y = 0;
      }
    } catch (_) {}
  }

  defaultParameterValue(id) {
    const core = this.model?.internalModel?.coreModel;
    if (!core) return null;
    try {
      const index = core.getParameterIndex?.(id);
      if (Number.isInteger(index) && index >= 0) return core.getParameterDefaultValue(index);
    } catch (_) {}
    try {
      return core.getParameterValueById?.(id);
    } catch (_) {
      return null;
    }
  }

  currentParameterValue(id) {
    const core = this.model?.internalModel?.coreModel;
    if (!core) return this.defaultParameterValue(id);
    try {
      return core.getParameterValueById?.(id);
    } catch (_) {
      return this.defaultParameterValue(id);
    }
  }

  resetMotionGroup(name, seconds = null) {
    const text = String(name || '');
    if (!isLive2DRuntimeResetName(text)) return false;
    const base = stripLive2DResetSuffix(text);
    const group = resolveLive2DGroup(base, base);
    const hasState = this.parameterLayers.has(group)
      || this.partOpacityLayers.has(group)
      || this.modelEffectLayers.has(group)
      || this.groupParameterIds.has(group)
      || this.activeMotionByGroup.has(group);
    if (!hasState) return false;
    this.fadeMotionGroupToBase(group, seconds);
    return true;
  }

  fadeMotionGroupToBase(groupName, seconds = null) {
    const group = resolveLive2DGroup(groupName, groupName);
    if (!group) return false;
    const generation = (this.motionGeneration.get(group) || 0) + 1;
    this.motionGeneration.set(group, generation);
    this.motionQueues.get(group)?.stopAllMotions?.();
    this.motionQueues.delete(group);

    const parameterLayer = this.parameterLayers.get(group);
    const partLayer = this.partOpacityLayers.get(group);
    const effectLayer = this.modelEffectLayers.get(group);
    const lastMotion = this.lastMotionByGroup.get(group);
    const authoredFadeOut = Number(lastMotion?.fadeOutTime ?? lastMotion?.data?.Meta?.FadeOutTime);
    const duration = seconds == null
      ? Math.max(0, Number.isFinite(authoredFadeOut) ? authoredFadeOut : 0.2)
      : Math.max(0, Number(seconds) || 0);

    const parameterStarts = new Map(parameterLayer || []);
    const parameterTargets = new Map();
    for (const id of parameterStarts.keys()) {
      parameterTargets.set(id, this.isSceneGroup(group)
        ? this.composedSceneBaseValue(id, { skipGroup: group })
        : (this.isAbsoluteOverrideGroup(group)
          ? this.composedParameterValue(id, { skipGroup: group })
          : 0));
    }
    const partStarts = new Map(partLayer || []);
    const partTargets = new Map();
    for (const id of partStarts.keys()) partTargets.set(id, this.composedPartOpacity(id, { skipGroup: group }));
    const effectStarts = new Map(effectLayer || []);

    const finish = () => {
      if (this.motionGeneration.get(group) !== generation) return;
      this.parameterLayers.delete(group);
      this.partOpacityLayers.delete(group);
      this.modelEffectLayers.delete(group);
      this.motionLayerReferences.delete(group);
      this.lastMotionByGroup.delete(group);
      this.activeMotionByGroup.delete(group);
      this.applyHeldParameters();
      this.applyHeldPartOpacities();
      this.applyHeldModelOpacity();
      this.updateMotionBadge();
    };

    if (duration <= 0 || (!parameterStarts.size && !partStarts.size && !effectStarts.size)) {
      finish();
      return true;
    }

    const started = performance.now();
    const tick = () => {
      if (!this.model || this.motionGeneration.get(group) !== generation) return;
      const elapsed = (performance.now() - started) / 1000;
      const weight = smoothStep(clamp(elapsed / duration, 0, 1));

      if (parameterLayer) {
        for (const [id, from] of parameterStarts.entries()) {
          const target = Number(parameterTargets.get(id));
          if (Number.isFinite(Number(from)) && Number.isFinite(target)) parameterLayer.set(id, lerp(Number(from), target, weight));
        }
      }
      if (partLayer) {
        for (const [id, from] of partStarts.entries()) {
          const target = Number(partTargets.get(id));
          if (Number.isFinite(Number(from)) && Number.isFinite(target)) partLayer.set(id, lerp(Number(from), target, weight));
        }
      }
      if (effectLayer) {
        for (const [id, from] of effectStarts.entries()) {
          const target = live2dModelCurveDefault(id);
          if (Number.isFinite(Number(from))) effectLayer.set(id, lerp(Number(from), target, weight));
        }
      }

      this.applyHeldParameters();
      this.applyHeldPartOpacities();
      this.applyHeldModelOpacity();
      if (weight < 1) requestAnimationFrame(tick);
      else finish();
    };
    tick();
    return true;
  }

  fadeLayerToDefaults(group, ids, seconds) {
    // Backwards-compatible entry point used by older call sites.
    return this.fadeMotionGroupToBase(group, seconds);
  }

  stopMotionGroupForName(name) {
    const motion = this.findMotionForTrigger(name);
    const base = stripLive2DResetSuffix(name);
    const group = motion ? this.motionGroup(motion) : resolveLive2DGroup(base, base);
    if (!group) return false;
    return this.fadeMotionGroupToBase(group, 0);
  }

  setLipSyncMode(mode) {
    const key = normalizeKey(mode || 'multi');
    if (key === 'off' || key === 'none' || key === 'false' || key === '0') this.lipSyncMode = 'off';
    else if (key === 'single' || key === 'normal' || key === 'default') this.lipSyncMode = 'single';
    else this.lipSyncMode = 'multi';
    // Kept for older call sites / inspector compatibility.
    this.lipSyncModeMulti = this.lipSyncMode === 'multi';
    if (this.lipSyncMode === 'off') this.stopLipSync(true);
  }

  playLipSync(voiceId, mode = this.lipSyncMode) {
    // Backward compatibility: the current app calls playLipSync(id, true/false).
    if (typeof mode === 'boolean') mode = mode ? 'multi' : 'single';
    // Match the original app.js contract: starting lip-sync must not depend
    // on _liveChannels already being registered. Some replay/message paths
    // register that channel after invoking playLipSync().
    let resolvedMode;
    if (typeof mode === 'boolean') resolvedMode = mode ? 'multi' : 'single';
    else resolvedMode = normalizeKey(mode || this.lipSyncMode || 'multi');
    if (resolvedMode === 'on' || resolvedMode === 'true' || resolvedMode === '1') resolvedMode = 'multi';
    if (resolvedMode === 'off' || resolvedMode === 'none' || resolvedMode === 'false' || resolvedMode === '0') {
      this.stopLipSync();
      return;
    }
    if (!['multi', 'single', 'normal', 'default'].includes(resolvedMode)) resolvedMode = 'multi';
    const suffix = extractLive2DVoiceSuffix(voiceId);
    const parameterId = this.resolveLipSyncParameterId(suffix, resolvedMode);
    if (!parameterId) {
      this.stopLipSync();
      return;
    }
    const now = performance.now();
    this.lipSync = {
      voiceId,
      suffix,
      parameterId,
      formParameterId: this.resolveLipSyncFormParameterId(parameterId, suffix),
      mode: resolvedMode,
      useMultiCharacterLipSync: true,
      mouseY: 0,
      mouseTargetY: 0,
      formY: 0,
      formTargetY: 0,
      voiced: 0,
      brightness: 0.35,
      consonant: 0,
      lastNow: now,
      startedAt: now,
      holdUntil: 0,
      weight: 1,
      releasing: false,
      releaseStartedAt: 0,
      releaseStartWeight: 1,
    };
  }

  stopLipSync(force = false) {
    const state = this.lipSync;
    if (!state) return;
    if (!force) {
      if (!state.releasing) {
        state.releasing = true;
        state.releaseStartedAt = performance.now();
        state.releaseStartWeight = clamp(Number(state.weight ?? 1), 0, 1);
        state.holdUntil = 0;
      }
      return;
    }
    this.lipSync = null;
    if (!this.live2dUpdateHookInstalled) this.applyHeldParameters();
  }

  clearLipSyncParameter(parameterId) {
    const layer = this.parameterLayers.get(L2D_LIP_SYNC.layer);
    if (!layer) return;
    layer.delete(parameterId);
  }

  updateLipSync() {
    const state = this.lipSync;
    if (!state?.parameterId) return;
    const now = performance.now();
    const dt = Math.min(0.05, Math.max(0.001, (now - state.lastNow) / 1000));
    state.lastNow = now;

    const sound = app.controller?.sound;
    const channelActive = !!window._liveChannels?.voice;
    const analyserActive = !!sound?.currentVoiceAnalyser?.();
    const ageSeconds = Math.max(0, (now - Number(state.startedAt || now)) / 1000);
    // Do not immediately cancel the state while the audio element is starting
    // or while a replay path is still registering its voice channel.
    if (!this.actualParameterExists(state.parameterId) ||
        (!channelActive && !analyserActive && ageSeconds > 0.45)) {
      if (!state.releasing) {
        state.releasing = true;
        state.releaseStartedAt = now;
        state.releaseStartWeight = clamp(Number(state.weight ?? 1), 0, 1);
        state.holdUntil = 0;
      }
    }

    let features = { opening: 0, voiced: 0, brightness: 0.35, consonant: 0 };
    if (!state.releasing) {
      features = sound?.currentVoiceLipFeatures?.() || features;
      // Last-resort compatibility with the original RMS-based app.js path.
      if (!(Number(features.opening) > 0)) {
        const opening = sound?.currentVoiceMouthOpening?.();
        if (Number.isFinite(Number(opening))) features.opening = Math.max(0, Number(opening));
      }
    }
    const raw = clamp(Number(features.opening) || 0, 0, 1);
    state.voiced = clamp(Number(features.voiced) || 0, 0, 1);
    state.brightness = clamp(Number(features.brightness) || 0.35, 0, 1);
    state.consonant = clamp(Number(features.consonant) || 0, 0, 1);

    // Speech articulation is not just amplitude. Voiced vowel regions get a
    // little more jaw opening, while high-frequency consonant/plosive regions
    // actively close the mouth. This produces visible syllable closures rather
    // than the old "volume meter = jaw angle" motion.
    let target = raw <= 0.018 ? 0 : clamp(Math.pow(raw, 0.86), 0, 1);
    target *= 1 + state.voiced * L2D_LIP_SYNC.voicedOpenBoost;
    target *= 1 - state.consonant * L2D_LIP_SYNC.consonantCloseStrength;
    target = clamp(target, 0, 1);

    if (!state.releasing) {
      if (state.consonant < 0.38 && target > state.mouseTargetY + 0.065) {
        state.holdUntil = now + L2D_LIP_SYNC.syllableHoldSec * 1000;
      }
      if (state.consonant < 0.32 && target < state.mouseTargetY && now < state.holdUntil) {
        target = Math.max(target, state.mouseTargetY * 0.64);
      }
    } else {
      target = 0;
      const releaseElapsed = Math.max(0, (now - Number(state.releaseStartedAt || now)) / 1000);
      const releaseT = clamp(releaseElapsed / Math.max(0.001, L2D_LIP_SYNC.endBlendSec), 0, 1);
      state.weight = Number(state.releaseStartWeight ?? 1) * (1 - smoothStep(releaseT));
    }
    if (!state.releasing) state.weight = 1;
    state.mouseTargetY = target;

    // On models that genuinely expose a continuous ParamMouthForm, add a very
    // small spectral vowel-shape motion. Discrete ParamMouthFxx/Nxx selectors
    // are deliberately never touched by LipSync.
    state.formTargetY = state.formParameterId && !state.releasing
      ? clamp((state.brightness - 0.42) * 2.1, -1, 1) * target
      : 0;

    const attack = L2D_LIP_SYNC.attackSec;
    let release = state.releasing
      ? Math.min(L2D_LIP_SYNC.tailReleaseSec, L2D_LIP_SYNC.endCloseSec)
      : L2D_LIP_SYNC.releaseSec;
    if (!state.releasing && state.consonant > 0.42 && target < state.mouseY) {
      release = Math.min(release, L2D_LIP_SYNC.consonantReleaseSec);
    }
    const tau = target > state.mouseY ? attack : release;
    const alpha = 1 - Math.exp(-dt / Math.max(0.001, tau));
    state.mouseY = lerp(state.mouseY, target, alpha);

    const formTau = Math.abs(state.formTargetY) > Math.abs(state.formY)
      ? L2D_LIP_SYNC.formAttackSec
      : L2D_LIP_SYNC.formReleaseSec;
    const formAlpha = 1 - Math.exp(-dt / Math.max(0.001, formTau));
    state.formY = lerp(state.formY, state.formTargetY, formAlpha);

    if (state.releasing && state.weight <= 0.002) {
      this.stopLipSync(true);
      return;
    }
  }

  fit() {
    if (!this.model) return;
    const w = el.stage.clientWidth || 1;
    const h = el.stage.clientHeight || 1;
    this.app?.renderer?.resize?.(w, h);

    if (app.aspect169) {
      const internal = this.model.internalModel;
      const canvasWidth = Number(internal?.originalWidth);
      const canvasHeight = Number(internal?.originalHeight);
      if (Number.isFinite(canvasWidth) && canvasWidth > 0 &&
          Number.isFinite(canvasHeight) && canvasHeight > 0) {
        const frame = this.fullscreenFrame();
        const scale = Math.max(frame.width / canvasWidth, frame.height / canvasHeight);
        this.model.scale.set(scale);
        this.model.x = frame.left + (frame.width - canvasWidth * scale) * 0.5;
        this.model.y = frame.top + (frame.height - canvasHeight * scale) * 0.5;
        this.updateCanvasClip();
        this.renderMosaicOverlay();
        return;
      }
    }

    const previousScaleX = this.model.scale?.x || 1;
    const previousScaleY = this.model.scale?.y || previousScaleX;
    this.model.scale?.set?.(1);
    let bounds = null;
    try {
      bounds = this.model.getLocalBounds?.();
    } catch (_) {
      bounds = null;
    }
    if (!bounds || !Number.isFinite(bounds.width) || !Number.isFinite(bounds.height) || bounds.width <= 0 || bounds.height <= 0) {
      bounds = {
        x: 0,
        y: 0,
        width: Math.max(1, this.model.width / previousScaleX),
        height: Math.max(1, this.model.height / previousScaleY),
      };
    }
    const scale = Math.min(w / Math.max(1, bounds.width), h / Math.max(1, bounds.height)) * 0.98;
    const scaleX = scale * LIVE2D_STAGE_SCALE_X;
    this.model.scale.set(scaleX, scale);
    this.model.x = w * 0.5 - (bounds.x + bounds.width * 0.5) * scaleX;
    this.model.y = h * 0.98 - (bounds.y + bounds.height) * scale;
    this.updateCanvasClip();
    this.renderMosaicOverlay();
  }
}


class NovelModelAsync {
  constructor() {
    this.reset();
  }

  reset() {
    this.pending = [];
    this.nextId = 1;
  }

  begin(tag, mode, commandName) {
    const item = {
      id: this.nextId,
      tag: tag || '',
      mode: mode || 'CONT',
      commandName: commandName || '',
      canceled: false,
      promise: null,
    };
    this.nextId += 1;
    this.pending.push(item);
    return item;
  }

  track(item, promise) {
    item.promise = Promise.resolve(promise)
      .catch((error) => {
        setStatus(`async command failed: ${error?.message || error}`, true);
      })
      .finally(() => {
        this.pending = this.pending.filter((entry) => entry !== item);
      });
    return item.promise;
  }

  isActive(item) {
    return !!item && !item.canceled && this.pending.includes(item);
  }

  wait(tag = '') {
    const key = String(tag || '').toLowerCase();
    const promises = this.pending
      .filter((entry) => !key || String(entry.tag).toLowerCase() === key)
      .map((entry) => entry.promise);
    return promises.length ? Promise.allSettled(promises) : Promise.resolve();
  }

  removeEndCommand() {
    this.pending = this.pending.filter((entry) => !entry.ended);
  }

  cancel(tag = '') {
    const key = String(tag || '').toLowerCase();
    for (const entry of this.pending) {
      if (!key || String(entry.tag).toLowerCase() === key) entry.canceled = true;
    }
    if (!key) this.pending = [];
    else this.pending = this.pending.filter((entry) => String(entry.tag).toLowerCase() !== key);
  }

  skip(tag = '') {
    this.removeEndCommand();
    this.cancel(tag);
  }

  stop(tag = '') {
    this.removeEndCommand();
    this.cancel(tag);
  }

  pendingLabels() {
    return this.pending.map((entry) => `${entry.mode}:${entry.tag || '(untagged)'}#${entry.id}`);
  }
}

class NovelCommandRegistry {
  constructor() {
    this.commands = new Map();
  }

  register(name, command) {
    this.commands.set(name.toLowerCase(), command);
    return this;
  }

  async execute(context, rawCommand) {
    const name = String(rawCommand.command || '').toLowerCase();
    const command = this.commands.get(name) || this.commands.get(baseCommandName(name)) || new NoopCommand();
    return command.execute(context, rawCommand, new NovelArguments(rawCommand));
  }
}

class NovelCommandBase {
  async execute(context, command, args) {
    return this.onExecute(context, command, args);
  }

  async onExecute() {
    return undefined;
  }
}

class NovelCommandAsyncBase extends NovelCommandBase {
  async runAsync(context, command, options, action) {
    if (context.replaying) {
      await action();
      return undefined;
    }

    const item = context.models.async.begin(options.tag, options.mode, command.command);
    const task = (async () => {
      if (!isImmediateDelay(options.delay)) await waitAsyncSeconds(options.delay, context.asyncToken);
      await this.runAsyncAction(context, options, action, item);
    })();
    context.models.async.track(item, task);
    return undefined;
  }

  async runAsyncAction(context, options, action, asyncItem) {
    if (!context.models.async.isActive(asyncItem)) return;
    if (context.asyncToken !== app.asyncToken) return;
    await action();
    if (!context.models.async.isActive(asyncItem)) return;
    context.models.scene.render();
    updateInspectors();
    if (options.duration > 0) await waitAsyncSeconds(options.duration, context.asyncToken);
    if (context.models.async.isActive(asyncItem)) asyncItem.ended = true;
  }

  asyncOptions(args, asyncCodeIndex, delayIndex, tagIndex = -1, duration = 0) {
    return {
      mode: parseAsyncMode(args.string(asyncCodeIndex, 'CONT')),
      delay: args.float(delayIndex, 0),
      tag: tagIndex > 0 ? args.string(tagIndex, '') : '',
      duration,
    };
  }
}

class NoopCommand extends NovelCommandBase {}

class LabelCommand extends NovelCommandBase {
  async onExecute(context, command) {
    context.models.scene.state.currentLabel = command.label || command.rawCommand?.replace(/^:/, '');
  }
}

class LabelJumpCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const label = args.string(1);
    const jumpIndex = context.player.labels.get(label.toLowerCase());
    return jumpIndex == null || context.replaying ? undefined : { jumpIndex };
  }
}

class PlotCommand extends NovelCommandBase {}

class EndOfCommand extends NovelCommandBase {}

class WaitCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    if (!context.replaying) await waitSeconds(args.float(1, 0), context.token);
  }
}

class WaitOrClickCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(1, 0);
    if (!context.replaying && seconds > 0) await waitSeconds(seconds, context.token);
  }
}

class AsyncWaitCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    if (context.replaying) return;
    const value = args.string(1);
    await context.models.async.wait(value);
  }
}

class AsyncSkipWaitCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.async.skip(args.string(1, ''));
  }
}

class AsyncStopWaitCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.async.stop(args.string(1, ''));
  }
}

class CleanAllCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.message.reset();
    context.models.screen.reset();
    context.models.scene.reset();
    context.models.async.reset();
  }
}

class CleanSkipCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.message.reset();
    context.models.screen.clearSkipArtifacts();
  }
}

class MessageCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const speaker = command.speaker || args.string(1);
    const text = command.message || args.string(2);
    const voiceCue = context.models.sound.consumeMessageVoice(command.voice || args.string(4));
    context.models.live2d.stopLipSync();
    context.models.message.show(speaker, text, voiceCue);
    
    if (window._liveChannels) delete window._liveChannels['voice'];

    if (voiceCue) {
      if (!context.replaying) {
        const voiceFactor = window.app?.config?.voiceVolume ?? 1.0;
        context.models.sound.play(voiceCue, 'voice', 'voice', { volume: voiceFactor });
        window._liveChannels = window._liveChannels || {};
        window._liveChannels['voice'] = {
          type: 'voice',
          origVolume: 1.0, 
          soundModel: context.models.sound
        };
      }
      context.models.live2d.playLipSync(voiceCue, context.models.live2d.lipSyncModeMulti);
    } else {
      context.models.live2d.stopLipSync();
    }
    return { pauseOnText: true };
  }
}

class L2DMessageCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const speaker = command.speaker || args.string(1);
    const text = command.message || args.string(2);
    const voiceCue = context.models.sound.consumeMessageVoice(command.voice || args.string(4));
    context.models.message.show(speaker, text, voiceCue);
    
    if (window._liveChannels) delete window._liveChannels['voice'];

    if (voiceCue) {
      if (!context.replaying) {
        const voiceFactor = window.app?.config?.voiceVolume ?? 1.0;
        context.models.sound.play(voiceCue, 'voice', 'voice', { volume: voiceFactor });
        window._liveChannels = window._liveChannels || {};
        window._liveChannels['voice'] = {
          type: 'voice',
          origVolume: 1.0, 
          soundModel: context.models.sound
        };
      }
      context.models.live2d.playLipSync(voiceCue, context.models.live2d.lipSyncModeMulti);
    } else {
      context.models.live2d.stopLipSync();
    }
    return { pauseOnText: true };
  }
}

class L2DLipSyncModeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.live2d.setLipSyncMode(args.string(1, 'multi'));
  }
}

class VoiceCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.sound.queueVoice(args.string(1), args.on(2, true));
  }
}

class VoiceStopCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.sound.stopVoiceAll();
    context.models.live2d.stopLipSync();
  }
}

class LoadVoiceCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const scriptId = args.string(1);
    if (!scriptId) return;
    context.models.sound.preloadCue(scriptId);
  }
}

class DotMessageCommand extends MessageCommand {}
class MessageTextCenterCommand extends MessageCommand {}

class MessageTextUnderCommand extends MessageTextCenterCommand {}

class TitleCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.message.show('', command.message || args.string(1));
    return { pauseOnText: true };
  }
}

class WindowCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const visible = args.on(1, false);
    const seconds = args.float(2, .5);
    context.models.message.setWindow(visible, seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class UiVisibleCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    document.body.dataset.uiVisible = String(args.on(1, true));
  }
}

class AdultUiCommand extends UiVisibleCommand {}
class MessageTextWindowCommand extends WindowCommand {}
class LineworkCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.screen.state.linework = { on: args.on(1, true), color: args.string(2), style: args.string(3), seconds: args.float(4, 0) };
  }
}

class FadeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    context.models.screen.fade(args.string(1, 'In'), args.string(2, 'Black'), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncFadeCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    const options = this.asyncOptions(args, 4, 5, -1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.screen.fade(args.string(1, 'In'), args.string(2, 'Black'), seconds);
    });
  }
}

class CrossFadeReadyCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.screen.captureCrossFade();
  }
}

class AsyncCrossFadeReadyCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 1, 2, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.screen.captureCrossFade();
    });
  }
}

class CrossFadeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(1, .5);
    context.models.screen.crossFade(context.replaying ? 0 : seconds, args.float(2, 0), args.float(3, 0), args.float(4, 1), args.float(5, 1));
    if (!context.replaying) await waitSeconds(seconds, context.token);
    context.models.screen.clearCrossFade();
  }
}

class AsyncCrossFadeCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(1, .5);
    const options = this.asyncOptions(args, 6, 7, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.screen.crossFade(context.replaying ? 0 : seconds, args.float(2, 0), args.float(3, 0), args.float(4, 1), args.float(5, 1));
      if (!context.replaying) await waitAsyncSeconds(seconds, context.asyncToken);
      context.models.screen.clearCrossFade();
    });
  }
}

class ColorFadeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(1, 1);
    context.models.screen.colorFade(seconds, args.float(2, 0), args.float(3, 0), args.float(4, 0), args.float(5, 0), args.float(6, 0));
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncColorFadeCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(1, 1);
    const options = this.asyncOptions(args, 7, 8, -1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.screen.colorFade(seconds, args.float(2, 0), args.float(3, 0), args.float(4, 0), args.float(5, 0), args.float(6, 0));
    });
  }
}

class TransitionFadeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(4, .5);
    context.models.screen.transitionFade(args.string(1, ''), args.string(2, 'Out'), args.string(3, 'Black'), context.replaying ? 0 : seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncTransitionFadeCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(4, .5);
    const options = this.asyncOptions(args, 5, 6, -1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.screen.transitionFade(args.string(1, ''), args.string(2, 'Out'), args.string(3, 'Black'), context.replaying ? 0 : seconds);
    });
  }
}

class TransitionCrossFadeReadyCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.screen.captureTransitionCrossFade();
  }
}

class AsyncTransitionCrossFadeReadyCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 1, 2, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.screen.captureTransitionCrossFade();
    });
  }
}

class TransitionCrossFadeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, .5);
    context.models.screen.transitionCrossFade(args.string(1, ''), context.replaying ? 0 : seconds);
    if (!context.replaying) {
      await waitSeconds(seconds, context.token);
      context.models.screen.clearTransitionCrossFade();
    }
  }
}

class AsyncTransitionCrossFadeCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, .5);
    const options = this.asyncOptions(args, 3, 4, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.screen.transitionCrossFade(args.string(1, ''), context.replaying ? 0 : seconds);
      if (!context.replaying) await waitAsyncSeconds(seconds, context.asyncToken);
      context.models.screen.clearTransitionCrossFade();
    });
  }
}

class BlurCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    context.models.screen.blur(args.on(1, false), args.string(2, ''), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncBlurCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    const options = this.asyncOptions(args, 4, 5, -1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.screen.blur(args.on(1, false), args.string(2, ''), seconds);
    });
  }
}

class ScreenEffectCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    context.models.screen.screenEffect(args.string(1), args.on(2, false), seconds, args.float(4, 1));
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncScreenEffectCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    const options = this.asyncOptions(args, 5, 6, 1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.screen.screenEffect(args.string(1), args.on(2, false), seconds, args.float(4, 1));
    });
  }
}

class Live2DInitCommand extends NovelCommandBase {
  async onExecute() {
    // Original command loads the R18 Live2D voice cue sheet and does not show the model.
  }
}

class L2DShowCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.live2d.show(args.string(1, command.model || LIVE2D_TAG));
    if (!context.replaying) await context.models.live2d.waitLoadComplete(context.token);
  }
}

class L2DHideCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.live2d.hide();
  }
}

class L2DMotionCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const motion = args.string(1, command.motion || '');
    const enabled = args.on(2, true);
    context.models.live2d.playMotion(motion, enabled);
  }
}

class AsyncL2DMotionCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const motion = args.string(1);
    const enabled = args.on(2, true);
    const options = this.asyncOptions(args, 3, 4);
    return this.runAsync(context, command, options, async () => {
      context.models.live2d.playMotion(motion, enabled);
    });
  }
}

class AsyncL2DHideCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 1, 2);
    return this.runAsync(context, command, options, async () => context.models.live2d.hide());
  }
}

class BgmPlayCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'bgm');
    const cue = args.string(2, command.cue || '');
    const fadeInSeconds = args.float(3, .75);
    const volume = args.float(4, 1);
    const endActionOn = args.on(5, true);
    if (context.replaying && !endActionOn) return;
    if (!cue) {
      context.models.sound.fadeChannel('bgm', tag, 0, 0);
      if (window._liveChannels) delete window._liveChannels[tag];
      return;
    }
    if (window._liveChannels && window._liveChannels[tag] && window._liveChannels[tag].currentCue === cue) {
      window._liveChannels[tag].origVolume = volume;
      const bgmFactor = window.app?.config?.bgmVolume ?? 1.0;
      const targetVolume = volume * bgmFactor;
      
      try {
        const soundModel = context.models.sound;
        if (typeof soundModel.fadeChannel === 'function') {
          soundModel.fadeChannel('bgm', tag, targetVolume, 0);
        } else if (typeof soundModel.fade === 'function') {
          soundModel.fade(tag, targetVolume, 0);
        }
        
        const ch = soundModel.channels?.[tag] || soundModel[tag];
        if (ch) ch.volume = targetVolume;
      } catch(e) {
      }
      
      return;
    }
    const bgmFactor = window.app?.config?.bgmVolume ?? 1.0;
    const finalVolume = volume * bgmFactor;
    context.models.sound.play(cue, 'bgm', tag, {
      volume: finalVolume,
      fadeInSeconds,
      loop: true,
    });
    window._liveChannels = window._liveChannels || {};
    window._liveChannels[tag] = {
      type: 'bgm',
      currentCue: cue,       
      origVolume: volume,
      soundModel: context.models.sound 
    };
  }
}

class AsyncBgmPlayCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'bgm');
    const cue = args.string(2, command.cue || '');
    const fadeInSeconds = args.float(3, .75);
    const volume = args.float(4, 1);
    const endActionOn = args.on(5, true);
    const options = this.asyncOptions(args, 6, 7);
    return this.runAsync(context, command, options, async () => {
      if (context.replaying && !endActionOn) return;
      if (!cue) {
        context.models.sound.fadeChannel('bgm', tag, 0, 0);
        if (window._liveChannels) delete window._liveChannels[tag];
        return;
      }
      if (window._liveChannels && window._liveChannels[tag] && window._liveChannels[tag].currentCue === cue) {
        window._liveChannels[tag].origVolume = volume;
        const bgmFactor = window.app?.config?.bgmVolume ?? 1.0;
        const targetVolume = volume * bgmFactor;
        
        try {
          const soundModel = context.models.sound;
          if (typeof soundModel.fadeChannel === 'function') {
            soundModel.fadeChannel('bgm', tag, targetVolume, 0);
          } else if (typeof soundModel.fade === 'function') {
            soundModel.fade(tag, targetVolume, 0);
          }
          const ch = soundModel.channels?.[tag] || soundModel[tag];
          if (ch) ch.volume = targetVolume;
        } catch(e) {
        }
        return;
      }
      const bgmFactor = window.app?.config?.bgmVolume ?? 1.0;
      const finalVolume = volume * bgmFactor;
      context.models.sound.play(cue, 'bgm', tag, { 
        volume: finalVolume, 
        fadeInSeconds, 
        loop: true 
      });
      window._liveChannels = window._liveChannels || {};
      window._liveChannels[tag] = {
        type: 'bgm',
        currentCue: cue,
        origVolume: volume,
        soundModel: context.models.sound
      };
    });
  }
}

class BgmPlayWorkunitCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'bgm');
    const workunit = args.string(2, '');
    const acbFile = args.string(3, '');
    const cue = args.string(4, command.cue || '');
    const fadeInSeconds = args.float(5, .75);
    const volume = args.float(6, 1);
    const endActionOn = args.on(7, true);
    if (context.replaying && !endActionOn) return;
    if (!workunit || !acbFile || !cue) {
      context.models.sound.fadeChannel('bgm', tag, 0, 0);
      if (window._liveChannels) delete window._liveChannels[tag];
      return;
    }
    if (window._liveChannels && window._liveChannels[tag] && window._liveChannels[tag].currentCue === cue) {
      window._liveChannels[tag].origVolume = volume;
      const bgmFactor = window.app?.config?.bgmVolume ?? 1.0;
      const targetVolume = volume * bgmFactor;
      
      try {
        const soundModel = context.models.sound;
        if (typeof soundModel.fadeChannel === 'function') {
          soundModel.fadeChannel('bgm', tag, targetVolume, 0);
        } else if (typeof soundModel.fade === 'function') {
          soundModel.fade(tag, targetVolume, 0);
        }
        const ch = soundModel.channels?.[tag] || soundModel[tag];
        if (ch) ch.volume = targetVolume;
      } catch(e) {
      }
      return;
    }
    const bgmFactor = window.app?.config?.bgmVolume ?? 1.0;
    const finalVolume = volume * bgmFactor;
    context.models.sound.play(cue, 'bgm', tag, { volume: finalVolume, fadeInSeconds, loop: true });
    window._liveChannels = window._liveChannels || {};
    window._liveChannels[tag] = {
      type: 'bgm',
      currentCue: cue,
      origVolume: volume,
      soundModel: context.models.sound
    };
  }
}

class AsyncBgmPlayWorkunitCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'bgm');
    const workunit = args.string(2, '');
    const acbFile = args.string(3, '');
    const cue = args.string(4, command.cue || '');
    const fadeInSeconds = args.float(5, .75);
    const volume = args.float(6, 1);
    const endActionOn = args.on(7, true);
    const options = this.asyncOptions(args, 8, 9);
    
    return this.runAsync(context, command, options, async () => {
      if (context.replaying && !endActionOn) return;
      if (!workunit || !acbFile || !cue) {
        context.models.sound.fadeChannel('bgm', tag, 0, 0);
        if (window._liveChannels) delete window._liveChannels[tag];
        return;
      }
      if (window._liveChannels && window._liveChannels[tag] && window._liveChannels[tag].currentCue === cue) {
        window._liveChannels[tag].origVolume = volume;
        const bgmFactor = window.app?.config?.bgmVolume ?? 1.0;
        const targetVolume = volume * bgmFactor;
        
        try {
          const soundModel = context.models.sound;
          if (typeof soundModel.fadeChannel === 'function') {
            soundModel.fadeChannel('bgm', tag, targetVolume, 0);
          } else if (typeof soundModel.fade === 'function') {
            soundModel.fade(tag, targetVolume, 0);
          }
          const ch = soundModel.channels?.[tag] || soundModel[tag];
          if (ch) ch.volume = targetVolume;
        } catch(e) {
        }
        return;
      }
      const bgmFactor = window.app?.config?.bgmVolume ?? 1.0;
      const finalVolume = volume * bgmFactor;
      context.models.sound.play(cue, 'bgm', tag, { volume: finalVolume, fadeInSeconds, loop: true });
      window._liveChannels = window._liveChannels || {};
      window._liveChannels[tag] = {
        type: 'bgm',
        currentCue: cue,
        origVolume: volume,
        soundModel: context.models.sound
      };
    });
  }
}

class BgmFadeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    if (context.replaying && !args.on(4, true)) return;
    context.models.sound.fadeChannel('bgm', args.string(1, 'bgm'), args.float(3, 1), args.float(2, .75));
  }
}

class AsyncBgmFadeCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'bgm');
    const seconds = args.float(2, .75);
    const volume = args.float(3, 1);
    const endActionOn = args.on(4, true);
    const options = this.asyncOptions(args, 5, 6);
    return this.runAsync(context, command, options, async () => {
      if (context.replaying && !endActionOn) return;
      context.models.sound.fadeChannel('bgm', tag, volume, seconds);
    });
  }
}

class BgmStopCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, '');
    if (context.replaying && !args.on(3, true)) return;
    if (!tag) return;
    context.models.sound.fadeChannel('bgm', isAllTag(tag) ? '' : tag, 0, args.float(2, .75));
  }
}

class AsyncBgmStopCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, '');
    const seconds = args.float(2, .75);
    const endActionOn = args.on(3, true);
    const options = this.asyncOptions(args, 4, 5);
    return this.runAsync(context, command, options, async () => {
      if (context.replaying && !endActionOn) return;
      if (tag) context.models.sound.fadeChannel('bgm', isAllTag(tag) ? '' : tag, 0, seconds);
    });
  }
}

class BgvPlayCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'bgv');
    const cue = args.string(2, command.cue || '');
    const volume = args.float(3, 0);
    const highlight = args.on(4, false);
    if (window._liveChannels && window._liveChannels[tag] && window._liveChannels[tag].currentCue === cue) {
      window._liveChannels[tag].origVolume = volume;
      const bgmFactor = window.app?.config?.bgmVolume ?? 1.0;
      const targetVolume = volume * bgmFactor; 
      try {
        const soundModel = context.models.sound;
        if (typeof soundModel.fadeChannel === 'function') {
          soundModel.fadeChannel('bgv', tag, targetVolume, 0);
        }
        const ch = soundModel.channels?.[tag] || soundModel[tag];
        if (ch) ch.volume = targetVolume;
      } catch(e) {}
      return;
    }
    const bgmFactor = window.app?.config?.bgmVolume ?? 1.0;
    const baseVolume = highlight ? 0 : volume;
    const finalVolume = baseVolume * bgmFactor;
    context.models.sound.play(cue, 'bgv', tag, {
      volume: finalVolume,
      loop: false,
    });
    if (highlight) {
      context.models.sound.fadeChannel('bgv', tag, volume * bgmFactor, 0);
    }
    window._liveChannels = window._liveChannels || {};
    window._liveChannels[tag] = {
      type: 'bgm', 
      currentCue: cue,
      origVolume: volume,
      soundModel: context.models.sound
    };
  }
}

class BgvFadeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.sound.fadeChannel('bgv', args.string(1, 'bgv'), args.float(3, 1), args.float(2, .75));
  }
}

class BgvStopCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.sound.fadeStop('bgv', args.string(1, ''), args.float(2, .75));
  }
}

class SePlayCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'se');
    const cue = args.string(2, command.cue || '');
    const fadeInSeconds = args.float(3, .75);
    const volume = args.float(4, 1);
    if (context.replaying) return;

    if (window._liveChannels && window._liveChannels[tag] && window._liveChannels[tag].currentCue === cue) {
      window._liveChannels[tag].origVolume = volume;
      const seFactor = window.app?.config?.seVolume ?? 1.0;
      const targetVolume = volume * seFactor;
      try {
        const soundModel = context.models.sound;
        if (typeof soundModel.fadeChannel === 'function') {
          soundModel.fadeChannel('se', tag, targetVolume, 0);
        } else if (typeof soundModel.fade === 'function') {
          soundModel.fade(tag, targetVolume, 0);
        }
        const ch = soundModel.channels?.[tag] || soundModel[tag];
        if (ch) ch.volume = targetVolume;
      } catch(e) {}
      return;
    }
    const seFactor = window.app?.config?.seVolume ?? 1.0;
    context.models.sound.play(cue, 'se', tag, { volume: volume * seFactor, fadeInSeconds });
    window._liveChannels = window._liveChannels || {};
    window._liveChannels[tag] = {
      type: 'se',
      currentCue: cue,
      origVolume: volume,
      soundModel: context.models.sound
    };
  }
}

class AsyncSePlayCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'se');
    const cue = args.string(2, command.cue || '');
    const fadeInSeconds = args.float(3, .75);
    const volume = args.float(4, 1);
    const options = this.asyncOptions(args, 5, 6);
    return this.runAsync(context, command, options, async () => {
      if (context.replaying) return;

      if (window._liveChannels && window._liveChannels[tag] && window._liveChannels[tag].currentCue === cue) {
        window._liveChannels[tag].origVolume = volume;
        const seFactor = window.app?.config?.seVolume ?? 1.0;
        const targetVolume = volume * seFactor;
        try {
          const soundModel = context.models.sound;
          if (typeof soundModel.fadeChannel === 'function') {
            soundModel.fadeChannel('se', tag, targetVolume, 0);
          } else if (typeof soundModel.fade === 'function') {
            soundModel.fade(tag, targetVolume, 0);
          }
          const ch = soundModel.channels?.[tag] || soundModel[tag];
          if (ch) ch.volume = targetVolume;
        } catch(e) {}
        return;
      }

      const seFactor = window.app?.config?.seVolume ?? 1.0;
      context.models.sound.play(cue, 'se', tag, { volume: volume * seFactor, fadeInSeconds });

      window._liveChannels = window._liveChannels || {};
      window._liveChannels[tag] = {
        type: 'se',
        currentCue: cue,
        origVolume: volume,
        soundModel: context.models.sound
      };
    });
  }
}

class SeFadeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    if (context.replaying && !args.on(4, true)) return;
    context.models.sound.fadeChannel('se', args.string(1, 'se'), args.float(3, 1), args.float(2, .75));
  }
}

class AsyncSeFadeCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'se');
    const seconds = args.float(2, .75);
    const volume = args.float(3, 1);
    const endActionOn = args.on(4, true);
    const options = this.asyncOptions(args, 5, 6);
    return this.runAsync(context, command, options, async () => {
      if (context.replaying && !endActionOn) return;
      context.models.sound.fadeChannel('se', tag, volume, seconds);
    });
  }
}

class SeStopCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    if (context.replaying && !args.on(3, true)) return;
    context.models.sound.fadeStop('se', args.string(1, ''), args.float(2, .75));
  }
}

class AsyncSeStopCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, '');
    const seconds = args.float(2, .75);
    const endActionOn = args.on(3, true);
    const options = this.asyncOptions(args, 4, 5);
    return this.runAsync(context, command, options, async () => {
      if (context.replaying && !endActionOn) return;
      context.models.sound.fadeStop('se', tag, seconds);
    });
  }
}

class CharaloadCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    await context.models.scene.loadCharacter(args.string(1, 'chara'), args.string(2), args.string(3));
  }
}

class CharaFaceCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.faceCharacter(args.string(1), args.string(2));
  }
}

class CharaPoseCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.poseCharacter(args.string(1), Number.parseInt(args.string(2, '0'), 10) || 0);
  }
}

class CharaMoveCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const moveType = args.string(2, 'Set');
    const seconds = normalizeCharaMoveType(moveType) === 'set' ? 0 : args.float(5, 1);
    context.models.scene.moveCharacter(args.string(1), moveType, args.string(3), args.float(4, 0), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class CharaScaleCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 0);
    context.models.scene.scaleCharacter(args.string(1), args.float(2, 1), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class CharaShowCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, .25);
    context.models.scene.showCharacter(args.string(1), seconds, 1);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class CharaShowAlphaCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, .25);
    context.models.scene.showCharacter(args.string(1), seconds, args.float(2, 1));
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class CharaHideCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, .25);
    context.models.scene.hideCharacter(args.string(1), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class CharaDeleteCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.deleteCharacter(args.string(1));
  }
}

class CharaEmoCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const tag = args.string(1);
    const duration = context.models.scene.emotionCharacter(tag, args.string(2));
    context.models.scene.render();
    if (context.isReplayTarget(command)) return;
    if (!context.replaying) await waitSeconds(duration, context.token);
    context.models.scene.clearEmotion(tag);
  }
}

class EmoDeleteCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.clearEmotion(args.string(1));
  }
}

class CharaFocusOnAllCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.scene.setAllCharacterFocus(true);
  }
}

class CharaFocusOnCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.focusCharacter(args.string(1), true);
  }
}

class CharaFocusOutCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.focusCharacter(args.string(1), false);
  }
}

class CharaFocusOutAllCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.scene.setAllCharacterFocus(false);
  }
}

class DefaultCharaColorCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.scene.setDefaultCharacterColor();
  }
}

class CharaMaskCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(4, 1);
    context.models.scene.maskCharacter(args.string(1), args.float(2, 0), args.float(3, 0), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class CharaItemCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(5, 0);
    context.models.scene.itemCharacter(args.string(1), Number(args.string(2, '0')), args.float(3, 0), args.float(4, 0), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class CharaReactionCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    context.models.scene.reactCharacter(args.string(1), args.string(2), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class CharaColorCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, 0);
    context.models.scene.colorCharacter(args.string(1), seconds, args.string(3, '0'), args.string(4, '0'), args.string(5, '0'), args.string(6, '255'));
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncCharaColorCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, 0);
    const options = this.asyncOptions(args, 7, 8, -1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.colorCharacter(args.string(1), seconds, args.string(3, '0'), args.string(4, '0'), args.string(5, '0'), args.string(6, '255'));
    });
  }
}

class AsyncCharaPoseCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 3, 4, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.poseCharacter(args.string(1), Number.parseInt(args.string(2, '0'), 10) || 0);
    });
  }
}

class AsyncCharaDeleteCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    return this.runAsync(context, command, this.asyncOptions(args, 2, 3, 1, 0), async () => {
      context.models.scene.deleteCharacter(args.string(1));
    });
  }
}

class AsyncCharaFocusOnCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    return this.runAsync(context, command, this.asyncOptions(args, 2, 3, -1, 0), async () => {
      context.models.scene.focusCharacter(args.string(1), true);
    });
  }
}

class AsyncCharaFocusOnAllCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    return this.runAsync(context, command, this.asyncOptions(args, 2, 3, -1, 0), async () => {
      context.models.scene.setAllCharacterFocus(true);
    });
  }
}

class AsyncCharaFocusOutCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    return this.runAsync(context, command, this.asyncOptions(args, 3, 4, -1, 0), async () => {
      context.models.scene.focusCharacter(args.string(1), false);
    });
  }
}

class AsyncCharaFocusOutAllCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    return this.runAsync(context, command, this.asyncOptions(args, 2, 3, -1, 0), async () => {
      context.models.scene.setAllCharacterFocus(false);
    });
  }
}

class AsyncCharaMaskCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(4, 1);
    return this.runAsync(context, command, this.asyncOptions(args, 5, 6, 1, seconds), async () => {
      context.models.scene.maskCharacter(args.string(1), args.float(2, 0), args.float(3, 0), seconds);
    });
  }
}

class AsyncCharaItemCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(5, 0);
    return this.runAsync(context, command, this.asyncOptions(args, 6, 7, 1, seconds), async () => {
      context.models.scene.itemCharacter(args.string(1), Number(args.string(2, '0')), args.float(3, 0), args.float(4, 0), seconds);
    });
  }
}

class PriorityCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.setPriority(args.string(1), args.string(2), args.string(3, '0'), args.string(4, ''), args.string(5, ''));
  }
}

class StillLoadCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.loadStill(args.string(1), args.string(2));
  }
}

class StillChangeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.changeStill(args.string(1), args.int(2, 1));
  }
}

class AsyncStillChangeCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 3, 4, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.changeStill(args.string(1), args.int(2, 1));
    });
  }
}

class StillShowCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, 0);
    context.models.scene.fadeStill(args.string(1), true, seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncStillShowCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, 0);
    const options = this.asyncOptions(args, 3, 4, -1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.fadeStill(args.string(1), true, seconds);
    });
  }
}

class StillHideCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, 0);
    context.models.scene.fadeStill(args.string(1), false, seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncStillHideCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, 0);
    const options = this.asyncOptions(args, 3, 4, -1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.fadeStill(args.string(1), false, seconds);
    });
  }
}

class StillDeleteCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.deleteStill(args.string(1));
  }
}

class AsyncStillDeleteCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 2, 3, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.deleteStill(args.string(1));
    });
  }
}

class SubimageCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.createSubImage(args.string(1), args.string(2), args.float(3, 0), args.string(4, ''), args.int(5, 0), args.string(6, ''), args.string(7, ''));
  }
}

class SubimageFadeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(4, 0);
    context.models.scene.fadeSubImage(args.string(1), args.string(2, '255'), args.string(3, '255'), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncSubimageFadeCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(4, 0);
    const options = this.asyncOptions(args, 5, 6, 1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.fadeSubImage(args.string(1), args.string(2, '255'), args.string(3, '255'), seconds);
    });
  }
}

class SubimageDeleteCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.deleteSubImage(args.string(1));
  }
}

class AsyncSubimageDeleteCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 2, 3, 1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.deleteSubImage(args.string(1));
    });
  }
}

class BgCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.setBackground(args.string(1), args.on(2, true));
  }
}

class BgColorCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(5, 0);
    context.models.scene.setBackgroundColor(args.string(1, '0'), args.string(2, '0'), args.string(3, '0'), args.string(4, '255'), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncBgColorCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(5, 0);
    const options = this.asyncOptions(args, 6, 7, -1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.setBackgroundColor(args.string(1, '0'), args.string(2, '0'), args.string(3, '0'), args.string(4, '255'), seconds);
    });
  }
}

class CameraMoveCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(4, 1);
    context.models.scene.moveCamera(args.string(1, 'Set'), args.string(2, 'X'), args.float(3, 1), seconds);
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncCameraMoveCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(4, 1);
    const options = this.asyncOptions(args, 5, 6, -1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.moveCamera(args.string(1, 'Set'), args.string(2, 'X'), args.float(3, 1), seconds);
    });
  }
}

class CameraZoomCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, 1);
    context.models.scene.zoomCamera(args.float(1, 1), seconds, args.string(3, 'Set'));
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncCameraZoomCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(2, 1);
    const options = this.asyncOptions(args, 4, 5, -1, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.zoomCamera(args.float(1, 1), seconds, args.string(3, 'Set'));
    });
  }
}

class MoveCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    context.models.scene.moveTarget(args.string(1), args.string(2), seconds, args.float(4, 0), args.float(5, 0), args.string(6, 'Set'));
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncMoveCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    const options = this.asyncOptions(args, 7, 8, 2, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.moveTarget(args.string(1), args.string(2), seconds, args.float(4, 0), args.float(5, 0), args.string(6, 'Set'));
    });
  }
}

class ScaleCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    context.models.scene.scaleTarget(args.string(1), args.string(2), seconds, args.float(4, 0), args.float(5, 0), args.string(6, 'Set'));
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncScaleCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    const options = this.asyncOptions(args, 7, 8, 2, seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.scaleTarget(args.string(1), args.string(2), seconds, args.float(4, 0), args.float(5, 0), args.string(6, 'Set'));
    });
  }
}

class JumpCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(4, 1);
    const count = args.int(5, 1);
    const loop = args.on(6, false);
    context.models.scene.jumpTarget(args.string(1), args.string(2), args.float(3, 0), seconds, count, loop);
    context.models.scene.render();
    if (!context.replaying && !loop) await waitSeconds(seconds * Math.max(1, count), context.token);
  }
}

class AsyncJumpCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const tag = args.string(2);
    const seconds = args.float(4, 1);
    const count = args.int(5, 1);
    const loop = args.on(6, false);
    if (loop) context.models.async.stop(tag);
    return this.runAsync(
      context,
      command,
      this.asyncOptions(args, 8, 7, 2, loop ? 0 : seconds * Math.max(1, count)),
      async () => context.models.scene.jumpTarget(args.string(1), tag, args.float(3, 0), seconds, count, loop),
    );
  }
}

class JumpStopCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.stopJumpTarget(args.string(1), args.string(2));
  }
}

class ShakeCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(5, 0);
    const count = args.int(6, 1);
    const loop = args.on(8, false);
    const shakeKind = args.int(9, 0);
    context.models.scene.shakeTarget(args.string(1), args.string(2), args.float(3, 0), args.float(4, 0), seconds, count, args.on(7, false), loop, shakeKind, args.on(10, false));
    if (!context.replaying && !loop) await waitSeconds(seconds, context.token);
  }
}

class AsyncShakeCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(5, 0);
    const count = args.int(6, 1);
    const loop = args.on(8, false);
    const shakeKind = args.int(9, 0);
    const options = this.asyncOptions(args, 11, 12, 2, loop ? 0 : seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.shakeTarget(args.string(1), args.string(2), args.float(3, 0), args.float(4, 0), seconds, count, args.on(7, false), loop, shakeKind, args.on(10, false));
    });
  }
}

class ShakeAllCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(5, 0);
    const count = args.int(4, 1);
    const loop = args.on(8, false);
    const shakeKind = args.int(7, 0);
    context.models.scene.shakeAll(args.float(1, 0), args.float(2, 0), args.float(3, 0), count, seconds, args.on(6, false), shakeKind, loop);
    if (!context.replaying && !loop) await waitSeconds(seconds, context.token);
  }
}

class AsyncShakeAllCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const seconds = args.float(5, 0);
    const count = args.int(4, 1);
    const loop = args.on(8, false);
    const shakeKind = args.int(7, 0);
    const options = this.asyncOptions(args, 9, 10, -1, loop ? 0 : seconds);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.shakeAll(args.float(1, 0), args.float(2, 0), args.float(3, 0), count, seconds, args.on(6, false), shakeKind, loop);
    });
  }
}

class ShakeAllStopCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.scene.stopShakeAll();
  }
}

class PrefabLoadCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.loadPrefab(args.string(1), args.string(2), args.string(3), args.int(4, 0), args.string(5, ''), args.string(6, ''));
  }
}

class PrefabShowCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(3, 1);
    context.models.scene.showPrefab(args.string(1), args.on(2, true), seconds, args.float(4, 0), args.float(5, 0), args.string(6, ''));
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncPrefabShowCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 2, 3, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.showPrefab(args.string(1), true, 0, args.float(4, 0), args.float(5, 0), args.string(6, ''));
    });
  }
}

class PrefabHideAllCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.showPrefabAll(args.string(1), '', false, 0, 0, '');
  }
}

class AsyncPrefabHideAllCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 2, 3, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.showPrefabAll(args.string(1), '', false, 0, 0, '');
    });
  }
}

class PrefabLoadUiCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.loadPrefabUi(args.string(1), args.string(2), args.string(3), args.int(4, 0), args.string(5, ''), args.string(6, ''));
  }
}

class PrefabShowUiCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.showPrefabUi(args.string(1), args.on(2, true), 0);
  }
}

class AsyncPrefabShowUiCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 4, 5, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.showPrefabUi(args.string(1), args.on(2, true), 0);
    });
  }
}

class PrefabShowAllCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const seconds = args.float(4, 0);
    context.models.scene.showPrefabAll(args.string(1), args.string(2), args.on(3, true), seconds, args.float(5, 0), args.string(6, ''));
    if (!context.replaying) await waitSeconds(seconds, context.token);
  }
}

class AsyncPrefabShowAllCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 4, 5, 1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.showPrefabAll(args.string(1), args.string(2), args.on(3, true), 0, args.float(6, 0), args.string(7, ''));
    });
  }
}

class PrefabDeleteUiCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.deletePrefabUi(args.string(1));
  }
}

class PrefabDeleteCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.deletePrefab(args.string(1));
  }
}

class PrefabDeleteAllCommand extends NovelCommandBase {
  async onExecute(context) {
    context.models.scene.deletePrefabAll();
  }
}

class ItemCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    context.models.scene.showItem(args.on(1, true), args.string(2));
  }
}

class SePlayIngameCommand extends NovelCommandBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'se');
    const cue = args.string(3, args.string(2, ''));
    const fadeInSeconds = args.float(4, .75);
    const volume = args.float(5, 1);
    if (context.replaying) return;

    if (window._liveChannels && window._liveChannels[tag] && window._liveChannels[tag].currentCue === cue) {
      window._liveChannels[tag].origVolume = volume;
      const seFactor = window.app?.config?.seVolume ?? 1.0;
      const targetVolume = volume * seFactor;
      try {
        const soundModel = context.models.sound;
        if (typeof soundModel.fadeChannel === 'function') {
          soundModel.fadeChannel('se', tag, targetVolume, 0);
        } else if (typeof soundModel.fade === 'function') {
          soundModel.fade(tag, targetVolume, 0);
        }
        const ch = soundModel.channels?.[tag] || soundModel[tag];
        if (ch) ch.volume = targetVolume;
      } catch(e) {}
      return;
    }

    const seFactor = window.app?.config?.seVolume ?? 1.0;
    context.models.sound.play(cue, 'se', tag, { volume: volume * seFactor, fadeInSeconds });

    window._liveChannels = window._liveChannels || {};
    window._liveChannels[tag] = {
      type: 'se',
      currentCue: cue,
      origVolume: volume,
      soundModel: context.models.sound
    };
  }
}

class AsyncSePlayIngameCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const tag = args.string(1, 'se');
    const cue = args.string(3, args.string(2, ''));
    const fadeInSeconds = args.float(4, .75);
    const volume = args.float(5, 1);
    const options = this.asyncOptions(args, 6, 7, 1, 0);
    return this.runAsync(context, command, options, async () => {
      if (context.replaying) return;

      if (window._liveChannels && window._liveChannels[tag] && window._liveChannels[tag].currentCue === cue) {
        window._liveChannels[tag].origVolume = volume;
        const seFactor = window.app?.config?.seVolume ?? 1.0;
        const targetVolume = volume * seFactor;
        try {
          const soundModel = context.models.sound;
          if (typeof soundModel.fadeChannel === 'function') {
            soundModel.fadeChannel('se', tag, targetVolume, 0);
          } else if (typeof soundModel.fade === 'function') {
            soundModel.fade(tag, targetVolume, 0);
          }
          const ch = soundModel.channels?.[tag] || soundModel[tag];
          if (ch) ch.volume = targetVolume;
        } catch(e) {}
        return;
      }

      const seFactor = window.app?.config?.seVolume ?? 1.0;
      context.models.sound.play(cue, 'se', tag, { volume: volume * seFactor, fadeInSeconds });

      window._liveChannels = window._liveChannels || {};
      window._liveChannels[tag] = {
        type: 'se',
        currentCue: cue,
        origVolume: volume,
        soundModel: context.models.sound
      };
    });
  }
}

class AsyncCharaMoveCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const moveType = args.string(2, 'Set');
    const duration = normalizeCharaMoveType(moveType) === 'set' ? 0 : args.float(5, 1);
    const options = this.asyncOptions(args, 6, 7, 1, duration);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.moveCharacter(args.string(1), moveType, args.string(3), args.float(4, 0), duration);
    });
  }
}

class AsyncCharaScaleCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const duration = args.float(3, 0);
    const options = this.asyncOptions(args, 4, 5, 1, duration);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.scaleCharacter(args.string(1), args.float(2, 1), duration);
    });
  }
}

class AsyncCharaShowCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const duration = args.float(2, .25);
    const options = this.asyncOptions(args, 3, 4, -1, duration);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.showCharacter(args.string(1), duration, 1);
    });
  }
}

class AsyncCharaShowAlphaCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const duration = args.float(3, .25);
    const options = this.asyncOptions(args, 4, 5, -1, duration);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.showCharacter(args.string(1), duration, args.float(2, 1));
    });
  }
}

class AsyncCharaHideCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const duration = args.float(2, .25);
    const options = this.asyncOptions(args, 3, 4, -1, duration);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.hideCharacter(args.string(1), duration);
    });
  }
}

class AsyncCharaFaceCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const options = this.asyncOptions(args, 3, 4, -1, 0);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.faceCharacter(args.string(1), args.string(2));
    });
  }
}

class AsyncCharaEmoCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const tag = args.string(1);
    const options = this.asyncOptions(args, 3, 4, -1, 0);
    return this.runAsync(context, command, options, async () => {
      const duration = context.models.scene.emotionCharacter(tag, args.string(2));
      context.models.scene.render();
      if (context.isReplayTarget(command)) return;
      if (context.replaying) {
        context.models.scene.clearEmotion(tag);
        return;
      }
      await waitAsyncSeconds(duration, context.asyncToken);
      context.models.scene.clearEmotion(tag);
    });
  }
}

class AsyncCharaReactionCommand extends NovelCommandAsyncBase {
  async onExecute(context, command, args) {
    const duration = args.float(3, 1);
    const options = this.asyncOptions(args, 4, 5, -1, duration);
    return this.runAsync(context, command, options, async () => {
      context.models.scene.reactCharacter(args.string(1), args.string(2), duration);
    });
  }
}

function buildCommandRegistry() {
  return new NovelCommandRegistry()
    .register(':label', new LabelCommand())
    .register('labeljump', new LabelJumpCommand())
    .register('plot', new PlotCommand())
    .register('endof', new EndOfCommand())
    .register('wait', new WaitCommand())
    .register('waitorclick', new WaitOrClickCommand())
    .register('asyncwait', new AsyncWaitCommand())
    .register('asyncskipwait', new AsyncSkipWaitCommand())
    .register('asyncstopwait', new AsyncStopWaitCommand())
    .register('cleanall', new CleanAllCommand())
    .register('cleanskip', new CleanSkipCommand())
    .register('initend', new NoopCommand())
    .register('message', new MessageCommand())
    .register('l2dmessage', new L2DMessageCommand())
    .register('messagetextcenter', new MessageTextCenterCommand())
    .register('messagetextunder', new MessageTextUnderCommand())
    .register('title', new TitleCommand())
    .register('window', new WindowCommand())
    .register('messagetextwindow', new MessageTextWindowCommand())
    .register('uivisible', new UiVisibleCommand())
    .register('adultui', new AdultUiCommand())
    .register('linework', new LineworkCommand())
    .register('fade', new FadeCommand())
    .register('asyncfade', new AsyncFadeCommand())
    .register('crossfadeready', new CrossFadeReadyCommand())
    .register('asynccrossfadeready', new AsyncCrossFadeReadyCommand())
    .register('crossfade', new CrossFadeCommand())
    .register('asynccrossfade', new AsyncCrossFadeCommand())
    .register('colorfade', new ColorFadeCommand())
    .register('asynccolorfade', new AsyncColorFadeCommand())
    .register('transitioncrossfadeready', new TransitionCrossFadeReadyCommand())
    .register('asynctransitioncrossfadeready', new AsyncTransitionCrossFadeReadyCommand())
    .register('transitioncrossfade', new TransitionCrossFadeCommand())
    .register('asynctransitioncrossfade', new AsyncTransitionCrossFadeCommand())
    .register('transitionfade', new TransitionFadeCommand())
    .register('asynctransitionfade', new AsyncTransitionFadeCommand())
    .register('blur', new BlurCommand())
    .register('asyncblur', new AsyncBlurCommand())
    .register('screeneffect', new ScreenEffectCommand())
    .register('asyncscreeneffect', new AsyncScreenEffectCommand())
    .register('live2dinit', new Live2DInitCommand())
    .register('l2dshow', new L2DShowCommand())
    .register('l2dhide', new L2DHideCommand())
    .register('l2dlipsyncmode', new L2DLipSyncModeCommand())
    .register('l2dmotion', new L2DMotionCommand())
    .register('asyncl2dmotion', new AsyncL2DMotionCommand())
    .register('asyncl2dhide', new AsyncL2DHideCommand())
    .register('bgmplay', new BgmPlayCommand())
    .register('asyncbgmplay', new AsyncBgmPlayCommand())
    .register('bgmplayworkunit', new BgmPlayWorkunitCommand())
    .register('asyncbgmplayworkunit', new AsyncBgmPlayWorkunitCommand())
    .register('bgmfade', new BgmFadeCommand())
    .register('asyncbgmfade', new AsyncBgmFadeCommand())
    .register('bgmstop', new BgmStopCommand())
    .register('asyncbgmstop', new AsyncBgmStopCommand())
    .register('bgvplay', new BgvPlayCommand())
    .register('bgvfade', new BgvFadeCommand())
    .register('bgvstop', new BgvStopCommand())
    .register('loadvoice', new LoadVoiceCommand())
    .register('voice', new VoiceCommand())
    .register('voicestop', new VoiceStopCommand())
    .register('seplay', new SePlayCommand())
    .register('asyncseplay', new AsyncSePlayCommand())
    .register('sefade', new SeFadeCommand())
    .register('asyncsefade', new AsyncSeFadeCommand())
    .register('seplayingame', new SePlayIngameCommand())
    .register('asyncseplayingame', new AsyncSePlayIngameCommand())
    .register('sestop', new SeStopCommand())
    .register('asyncsestop', new AsyncSeStopCommand())
    .register('charaload', new CharaloadCommand())
    .register('charadelete', new CharaDeleteCommand())
    .register('charaemo', new CharaEmoCommand())
    .register('charaface', new CharaFaceCommand())
    .register('charafocuson', new CharaFocusOnCommand())
    .register('charafocusout', new CharaFocusOutCommand())
    .register('charafocusoutall', new CharaFocusOutAllCommand())
    .register('charapose', new CharaPoseCommand())
    .register('charamove', new CharaMoveCommand())
    .register('charamask', new CharaMaskCommand())
    .register('charaitem', new CharaItemCommand())
    .register('charareaction', new CharaReactionCommand())
    .register('charascale', new CharaScaleCommand())
    .register('characolor', new CharaColorCommand())
    .register('charashow', new CharaShowCommand())
    .register('charahide', new CharaHideCommand())
    .register('charashowalpha', new CharaShowAlphaCommand())
    .register('charafocusonall', new CharaFocusOnAllCommand())
    .register('defaultcharacolor', new DefaultCharaColorCommand())
    .register('emodelete', new EmoDeleteCommand())
    .register('bg', new BgCommand())
    .register('bgcolor', new BgColorCommand())
    .register('asyncbgcolor', new AsyncBgColorCommand())
    .register('cameramove', new CameraMoveCommand())
    .register('asynccameramove', new AsyncCameraMoveCommand())
    .register('camerazoom', new CameraZoomCommand())
    .register('asynccamerazoom', new AsyncCameraZoomCommand())
    .register('move', new MoveCommand())
    .register('asyncmove', new AsyncMoveCommand())
    .register('scale', new ScaleCommand())
    .register('asyncscale', new AsyncScaleCommand())
    .register('priority', new PriorityCommand())
    .register('stillload', new StillLoadCommand())
    .register('stillchange', new StillChangeCommand())
    .register('asyncstillchange', new AsyncStillChangeCommand())
    .register('stillshow', new StillShowCommand())
    .register('asyncstillshow', new AsyncStillShowCommand())
    .register('stillhide', new StillHideCommand())
    .register('asyncstillhide', new AsyncStillHideCommand())
    .register('stilldelete', new StillDeleteCommand())
    .register('asyncstilldelete', new AsyncStillDeleteCommand())
    .register('subimage', new SubimageCommand())
    .register('subimagefade', new SubimageFadeCommand())
    .register('asyncsubimagefade', new AsyncSubimageFadeCommand())
    .register('subimagedelete', new SubimageDeleteCommand())
    .register('asyncsubimagedelete', new AsyncSubimageDeleteCommand())
    .register('prefabload', new PrefabLoadCommand())
    .register('prefabshow', new PrefabShowCommand())
    .register('asyncprefabshow', new AsyncPrefabShowCommand())
    .register('prefabhideall', new PrefabHideAllCommand())
    .register('asyncprefabhideall', new AsyncPrefabHideAllCommand())
    .register('prefabloadui', new PrefabLoadUiCommand())
    .register('prefabuiload', new PrefabLoadUiCommand())
    .register('prefabshowui', new PrefabShowUiCommand())
    .register('asyncprefabshowui', new AsyncPrefabShowUiCommand())
    .register('prefabshowall', new PrefabShowAllCommand())
    .register('asyncprefabshowall', new AsyncPrefabShowAllCommand())
    .register('prefabdelete', new PrefabDeleteCommand())
    .register('prefabdeleteall', new PrefabDeleteAllCommand())
    .register('prefabdeleteui', new PrefabDeleteUiCommand())
    .register('item', new ItemCommand())
    .register('jump', new JumpCommand())
    .register('jumpstop', new JumpStopCommand())
    .register('shake', new ShakeCommand())
    .register('shakeall', new ShakeAllCommand())
    .register('asyncshakeall', new AsyncShakeAllCommand())
    .register('shakeallstop', new ShakeAllStopCommand())
    .register('asyncjump', new AsyncJumpCommand())
    .register('asyncshake', new AsyncShakeCommand())
    .register('asynccharamove', new AsyncCharaMoveCommand())
    .register('asynccharascale', new AsyncCharaScaleCommand())
    .register('asynccharacolor', new AsyncCharaColorCommand())
    .register('asynccharapose', new AsyncCharaPoseCommand())
    .register('asynccharadelete', new AsyncCharaDeleteCommand())
    .register('asynccharafocuson', new AsyncCharaFocusOnCommand())
    .register('asynccharafocusonall', new AsyncCharaFocusOnAllCommand())
    .register('asynccharafocusout', new AsyncCharaFocusOutCommand())
    .register('asynccharafocusoutall', new AsyncCharaFocusOutAllCommand())
    .register('asynccharamask', new AsyncCharaMaskCommand())
    .register('asynccharaitem', new AsyncCharaItemCommand())
    .register('asynccharashow', new AsyncCharaShowCommand())
    .register('asynccharashowalpha', new AsyncCharaShowAlphaCommand())
    .register('asynccharahide', new AsyncCharaHideCommand())
    .register('asynccharaface', new AsyncCharaFaceCommand())
    .register('asynccharaemo', new AsyncCharaEmoCommand())
    .register('asynccharareaction', new AsyncCharaReactionCommand());
}

function isTextPauseCommand(commandName) {
  return new Set(['message', 'l2dmessage', 'messagetextcenter', 'messagetextunder', 'title']).has(String(commandName || '').toLowerCase());
}

async function fetchJson(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

async function fetchJsonOptional(url, fallback) {
  try {
    return await fetchJson(url);
  } catch (_) {
    return fallback;
  }
}

function baseCommandName(commandName) {
  const name = String(commandName || '').toLowerCase();
  return name.startsWith('async') ? name.slice(5) : name;
}

function parseAsyncMode(value) {
  return String(value || '').trim().toUpperCase() === 'STOP' ? 'STOP' : 'CONT';
}

function parseOnOff(value, fallback = false) {
  const text = String(value ?? '').trim().toLowerCase();
  if (['on', 'true', '1'].includes(text)) return true;
  if (['off', 'false', '0'].includes(text)) return false;
  return fallback;
}

function normalizeTargetType(value) {
  const key = normalizeAssetKey(value);
  if (['back', 'bg', 'background'].includes(key)) return 'back';
  if (['chara', 'character'].includes(key)) return 'chara';
  if (['subimage', 'still'].includes(key)) return 'subimage';
  if (['prefab', 'prefabui', 'ui', 'other'].includes(key)) return 'prefabui';
  return key || 'object';
}

function byteToUnit(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  if (numeric > 1) return clamp(numeric / 255, 0, 1);
  return clamp(numeric, 0, 1);
}

function normalizeAlpha(value) {
  return byteToUnit(value);
}

function rgbaByte(r, g, b, alpha = 255) {
  const rr = clamp(Math.round(Number(r) || 0), 0, 255);
  const gg = clamp(Math.round(Number(g) || 0), 0, 255);
  const bb = clamp(Math.round(Number(b) || 0), 0, 255);
  return `rgba(${rr},${gg},${bb},${byteToUnit(alpha)})`;
}

function rgbaFloat(r, g, b, alpha = 1) {
  const rr = clamp(Number(r) || 0, 0, 1) * 255;
  const gg = clamp(Number(g) || 0, 0, 1) * 255;
  const bb = clamp(Number(b) || 0, 0, 1) * 255;
  return `rgba(${rr},${gg},${bb},${clamp(Number(alpha) || 0, 0, 1)})`;
}

function easingToCss(value) {
  const key = normalizeAssetKey(value || 'linear');
  if (key.includes('easeinout')) return 'ease-in-out';
  if (key.includes('easein')) return 'ease-in';
  if (key.includes('easeout')) return 'ease-out';
  return 'linear';
}

function screenEffectFilter(effect, value) {
  const key = normalizeAssetKey(effect);
  const amount = Number.isFinite(Number(value)) ? Number(value) : 1;
  if (key === 'bloom') return `brightness(${1 + Math.max(0, amount) * 0.08}) saturate(${1 + Math.max(0, amount) * 0.12})`;
  if (key.includes('blur')) return `blur(${Math.max(0, amount) * 4}px)`;
  if (key.includes('gray') || key.includes('mono')) return 'grayscale(1)';
  if (key.includes('sepia')) return 'sepia(1)';
  if (key.includes('invert')) return 'invert(1)';
  return `brightness(${1 + Math.max(0, amount) * 0.04})`;
}

function characterFilter(item) {
  const parts = [];
  if (item.focus === false) parts.push('brightness(.62) saturate(.82)');
  if (item.color) {
    const a = normalizeAlpha(item.color.alpha);
    const brightness = (0.2126 * byteToUnit(item.color.r)) + (0.7152 * byteToUnit(item.color.g)) + (0.0722 * byteToUnit(item.color.b));
    parts.push(`brightness(${clamp(brightness * 1.8, 0.05, 2)}) opacity(${a})`);
  }
  return parts.join(' ');
}

function mergeTransition(current, nextTransformTransition) {
  const kept = String(current || '')
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part && !part.startsWith('transform '));
  kept.push(nextTransformTransition);
  return kept.join(', ');
}

function resolveSceneAssetImage(asset) {
  return resolveSceneAssetInfo(asset).src;
}

function resolveSceneAssetInfo(asset) {
  const text = String(asset || '');
  const leaf = assetLeaf(text);
  const bgKey = normalizeAssetKey(leaf);
  const isNovelBgPath = /ui\/bg\/novel/i.test(text);
  if (/^abg\d+[a-z]?$/i.test(leaf) || isNovelBgPath) {
    const meta = app.backgroundAssets.get(bgKey);
    const width = Number(meta?.width) || 0;
    const height = Number(meta?.height) || 0;
    return {
      src: `${BG_DATA_ROOT}${bgKey}.png`,
      width,
      height,
      fullFrame: isNovelBgPath || /^abg\d+[a-z]?$/i.test(leaf) || (width >= 512 && height >= 512),
    };
  }
  return { src: '', width: 0, height: 0, fullFrame: false };
}

function assetLeaf(asset) {
  return String(asset || '').split(/[\\/]/).filter(Boolean).pop() || '';
}

function normalizeAssetKey(value) {
  return String(value || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function normalizeAudioCueKey(value) {
  return String(value || '').trim().toLowerCase().replace(/[^a-z0-9_.-]+/g, '_').replace(/^_+|_+$/g, '');
}

function isThoughtMessage(value) {
  const text = String(value || '').replace(/<br\s*\/?>/gi, '').trim();
  if (!text) return false;
  return (text.startsWith('（') && text.includes('）')) || (text.startsWith('(') && text.includes(')'));
}

function screenBlurTargetLayers(target) {
  if (target === 'back' || target === 'bg' || target === 'background') return [el.bgLayer];
  if (target === 'chara' || target === 'character') return [el.sceneLayer];
  if (target === 'l2d' || target === 'live2d') return [el.live2dCanvas, el.fallbackTexture];
  return [el.bgLayer, el.live2dCanvas, el.fallbackTexture, el.sceneLayer];
}

function normalizeCharaFace(value) {
  const key = normalizeAssetKey(value);
  if (!key) return 'normal';
  if (key === 'eyeclose') return 'closed';
  if (key === 'eyeopen') return 'eyeopen';
  if (key.startsWith('face')) return key.slice(4) || 'normal';
  return key;
}

function currentCharaFaceKey(item) {
  if (item.eyeClosed && item.asset?.faces?.closed) return 'closed';
  return item.baseFace || 'normal';
}

function resolveCharaMoveParameter(parameter, value, character) {
  const key = normalizeAssetKey(parameter);
  const numeric = Number(value);
  const finalValue = Number.isFinite(numeric) ? numeric : 0;
  if (key === 'x') return { axis: 'x', value: finalValue };
  if (key === 'y') return { axis: 'y', value: finalValue };
  if (key === 'leftd') return { axis: 'x', value: finalValue - 200 };
  if (key === 'rightd') return { axis: 'x', value: finalValue + 200 };
  if (key === 'leftt') return { axis: 'x', value: finalValue - 300 };
  if (key === 'rightt') return { axis: 'x', value: finalValue + 300 };
  if (key === 'charaheight') return { axis: 'y', value: characterDefaultHeight(character) + finalValue };
  return { axis: 'x', value: finalValue };
}

function normalizeCharaMoveType(value) {
  return normalizeAssetKey(value || 'set');
}

function resolveCharaMoveValue(moveType, value, current) {
  const type = normalizeCharaMoveType(moveType);
  const original = Number(current);
  const base = Number.isFinite(original) ? original : 0;
  const amount = Number.isFinite(Number(value)) ? Number(value) : 0;
  if (type === 'set' || type === 'lineartarget' || type === 'easeintarget' || type === 'easeouttarget') {
    return { value: amount, instant: type === 'set' };
  }
  if (type === 'add' || type === 'linear' || type === 'easein' || type === 'easeout') {
    return { value: base + amount, instant: type === 'add' };
  }
  return { value: base, instant: true };
}

function charaMoveEasing(value) {
  const type = normalizeCharaMoveType(value);
  if (type === 'easein' || type === 'easeintarget') return 'ease-in';
  if (type === 'easeout') return 'ease-out';
  if (type === 'easeouttarget') return 'ease-out';
  return 'linear';
}

function characterDefaultHeight(character) {
  const value = Number(character?.asset?.defaultHeight ?? character?.defaultHeight);
  return Number.isFinite(value) ? value : 0;
}

function rectSize(rect, fallbackX, fallbackY) {
  const size = rect?.sizeDelta || {};
  const x = Number(size.x);
  const y = Number(size.y);
  return {
    x: Number.isFinite(x) && Math.abs(x) > 0.0001 ? Math.abs(x) : fallbackX,
    y: Number.isFinite(y) && Math.abs(y) > 0.0001 ? Math.abs(y) : fallbackY,
  };
}

function rectPosition(rect) {
  const position = rect?.anchoredPosition || {};
  return {
    x: Number(position.x) || 0,
    y: Number(position.y) || 0,
  };
}

function rectWorldPosition(rect) {
  const position = rect?.worldPosition || rect?.anchoredPosition || {};
  return {
    x: Number(position.x) || 0,
    y: Number(position.y) || 0,
  };
}

function rectScale(rect, fallback) {
  const scale = rect?.localScale || {};
  return {
    x: Number(scale.x) || fallback,
    y: Number(scale.y) || fallback,
  };
}

function rectPivot(rect) {
  const pivot = rect?.pivot || {};
  const x = Number(pivot.x);
  const y = Number(pivot.y);
  return {
    x: Number.isFinite(x) ? x : 0.5,
    y: Number.isFinite(y) ? y : 0.5,
  };
}

function emotionAttachmentTransform(asset) {
  const emotionRect = asset?.emotionRect || {};
  const zoomRect = asset?.zoomRect || {};
  return {
    position: rectPosition(emotionRect),
    scale: rectScale(zoomRect, 1),
    pivot: rectPivot(emotionRect),
  };
}

function cssEscape(value) {
  if (window.CSS?.escape) return CSS.escape(value);
  return String(value).replace(/["\\]/g, '\\$&');
}

function isImmediateDelay(value) {
  const seconds = Math.max(0, Number(value) || 0);
  return seconds <= 0.000001;
}

function evaluateMotionCurve(segments, time) {
  if (!Array.isArray(segments) || segments.length < 2) return null;
  let previousTime = Number(segments[0]) || 0;
  let previousValue = Number(segments[1]) || 0;
  if (time <= previousTime) return previousValue;

  let i = 2;
  while (i < segments.length) {
    const type = Number(segments[i++]) || 0;
    if (type === 0 || type === 2 || type === 3) {
      const nextTime = Number(segments[i++]);
      const nextValue = Number(segments[i++]);
      if (!Number.isFinite(nextTime) || !Number.isFinite(nextValue)) break;
      if (time <= nextTime) {
        if (type === 2) return previousValue;
        if (type === 3) return nextValue;
        const span = Math.max(0.000001, nextTime - previousTime);
        const t = clamp((time - previousTime) / span, 0, 1);
        return previousValue + (nextValue - previousValue) * t;
      }
      previousTime = nextTime;
      previousValue = nextValue;
      continue;
    }

    if (type === 1) {
      const cp1Time = Number(segments[i++]);
      const cp1Value = Number(segments[i++]);
      const cp2Time = Number(segments[i++]);
      const cp2Value = Number(segments[i++]);
      const endTime = Number(segments[i++]);
      const endValue = Number(segments[i++]);
      if (!Number.isFinite(endTime) || !Number.isFinite(endValue)) break;
      if (time <= endTime) {
        return cubicBezierValueAtTime(
          previousTime,
          previousValue,
          cp1Time,
          cp1Value,
          cp2Time,
          cp2Value,
          endTime,
          endValue,
          time,
        );
      }
      previousTime = endTime;
      previousValue = endValue;
      continue;
    }

    break;
  }
  return previousValue;
}

function cubicBezierValueAtTime(startTime, startValue, cp1Time, cp1Value, cp2Time, cp2Value, endTime, endValue, time) {
  const span = Math.max(0.000001, endTime - startTime);
  const target = clamp((time - startTime) / span, 0, 1);
  const x1 = clamp((cp1Time - startTime) / span, 0, 1);
  const x2 = clamp((cp2Time - startTime) / span, 0, 1);
  let lo = 0;
  let hi = 1;
  let t = target;
  for (let i = 0; i < 12; i += 1) {
    const x = cubicBezierValue(0, x1, x2, 1, t);
    if (x < target) lo = t;
    else hi = t;
    t = (lo + hi) * 0.5;
  }
  return cubicBezierValue(startValue, cp1Value, cp2Value, endValue, t);
}

function cubicBezierValue(p0, p1, p2, p3, t) {
  const u = 1 - t;
  return (u * u * u * p0) + (3 * u * u * t * p1) + (3 * u * t * t * p2) + (t * t * t * p3);
}

function isAllTag(tag) {
  const key = normalizeKey(tag);
  return key === 'all' || key === '*';
}

function waitSeconds(seconds, token) {
  return waitSecondsWhile(seconds, () => token === app.runToken);
}

function waitAsyncSeconds(seconds, token) {
  return waitSecondsWhile(seconds, () => token === app.asyncToken);
}

function waitFrame() {
  return new Promise((resolve) => requestAnimationFrame(resolve));
}

function waitSecondsWhile(seconds, isActive) {
  const ms = Math.max(0, Number(seconds) || 0) * 1000;
  if (!ms || !isActive() || app.fastForward) return Promise.resolve();
  return new Promise((resolve) => {
    const id = setTimeout(resolve, ms);
    const check = () => {
      if (!isActive() || app.fastForward) {
        clearTimeout(id);
        resolve();
      } else {
        requestAnimationFrame(check);
      }
    };
    requestAnimationFrame(check);
  });
}

function fadeAudio(audio, targetVolume, seconds, done) {
  const start = audio.volume;
  const end = clamp(targetVolume, 0, 1);
  const duration = Math.max(0, seconds) * 1000;
  if (!duration) {
    audio.volume = end;
    done?.();
    return;
  }
  const started = performance.now();
  const tick = () => {
    const t = clamp((performance.now() - started) / duration, 0, 1);
    audio.volume = start + (end - start) * t;
    if (t < 1) requestAnimationFrame(tick);
    else done?.();
  };
  requestAnimationFrame(tick);
}

function setStatus(text, error = false) {
  el.statusLine.textContent = text || '';
  el.statusLine.style.color = error ? 'var(--danger)' : 'var(--muted)';
}

function pulseFallback() {
  el.fallbackTexture.animate([
    { transform: 'scale(1)' },
    { transform: 'scale(1.018)' },
    { transform: 'scale(1)' },
  ], { duration: 320, easing: 'ease-out' });
}

function resizeOverlayCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round((el.stage.clientWidth || 1) * dpr));
  const height = Math.max(1, Math.round((el.stage.clientHeight || 1) * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  return dpr;
}

function pixelateCanvasMesh(source, target, scratch, mesh, blockSize) {
  const rect = mesh.rect || {};
  const x = clamp(rect.x, 0, target.width);
  const y = clamp(rect.y, 0, target.height);
  const width = clamp(rect.width, 0, target.width - x);
  const height = clamp(rect.height, 0, target.height - y);
  if (width <= 1 || height <= 1) return;

  const smallWidth = Math.max(1, Math.ceil(width / Math.max(1, blockSize)));
  const smallHeight = Math.max(1, Math.ceil(height / Math.max(1, blockSize)));
  scratch.width = smallWidth;
  scratch.height = smallHeight;
  const scratchCtx = scratch.getContext('2d', { willReadFrequently: true });
  const targetCtx = target.getContext('2d', { willReadFrequently: true });
  if (!scratchCtx || !targetCtx) return;

  scratchCtx.imageSmoothingEnabled = true;
  scratchCtx.clearRect(0, 0, smallWidth, smallHeight);
  scratchCtx.drawImage(source, x, y, width, height, 0, 0, smallWidth, smallHeight);

  targetCtx.save();
  buildMeshClipPath(targetCtx, mesh.points, mesh.indices);
  targetCtx.clip();
  targetCtx.imageSmoothingEnabled = false;
  targetCtx.drawImage(scratch, 0, 0, smallWidth, smallHeight, x, y, width, height);
  targetCtx.imageSmoothingEnabled = true;
  targetCtx.restore();
}

function buildMeshClipPath(ctx, points, indices) {
  ctx.beginPath();
  for (let i = 0; i + 2 < indices.length; i += 3) {
    const a = points[indices[i]];
    const b = points[indices[i + 1]];
    const c = points[indices[i + 2]];
    if (!a || !b || !c) continue;
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.lineTo(c.x, c.y);
    ctx.closePath();
  }
}

function buildTransitionSnapshot() {
  const snapshot = document.createElement('div');
  snapshot.className = 'transition-snapshot';
  snapshot.appendChild(cloneTransitionLayer(el.bgLayer, 'transition-bg-copy'));

  const canvasCopy = captureCanvasImage(el.live2dCanvas);
  if (canvasCopy) snapshot.appendChild(canvasCopy);
  const mosaicCopy = captureCanvasImage(el.mosaicLayer);
  if (mosaicCopy) snapshot.appendChild(mosaicCopy);
  snapshot.appendChild(cloneTransitionLayer(el.fallbackTexture, 'transition-fallback-copy'));
  snapshot.appendChild(cloneTransitionLayer(el.sceneLayer, 'transition-scene-copy'));
  snapshot.appendChild(cloneTransitionLayer(el.screenEffectLayer, 'transition-screen-effect-copy'));
  return snapshot;
}

function cloneTransitionLayer(source, extraClass) {
  const clone = source.cloneNode(true);
  stripIds(clone);
  clone.classList.add('transition-layer-copy');
  if (extraClass) clone.classList.add(extraClass);
  clone.style.position = 'absolute';
  clone.style.inset = '0';
  clone.style.pointerEvents = 'none';

  const computed = getComputedStyle(source);
  clone.style.opacity = computed.opacity;
  clone.style.filter = computed.filter === 'none' ? '' : computed.filter;
  clone.style.display = computed.display;
  if (source === el.bgLayer) {
    clone.style.backgroundImage = computed.backgroundImage;
    clone.style.backgroundPosition = computed.backgroundPosition;
    clone.style.backgroundRepeat = computed.backgroundRepeat;
    clone.style.backgroundSize = computed.backgroundSize;
  }
  if (source === el.fallbackTexture) {
    clone.style.width = computed.width;
    clone.style.height = computed.height;
    clone.style.maxWidth = computed.maxWidth;
    clone.style.objectFit = computed.objectFit;
    clone.style.margin = computed.margin;
  }
  return clone;
}

function captureCanvasImage(canvas) {
  if (!canvas || !canvas.width || !canvas.height) return null;
  try {
    const source = canvas.toDataURL('image/png');
    if (!source || source.length < 128) return null;
    const image = document.createElement('img');
    image.className = 'transition-canvas-copy';
    image.src = source;
    const computed = getComputedStyle(canvas);
    image.style.clipPath = computed.clipPath === 'none' ? '' : computed.clipPath;
    image.style.webkitClipPath = computed.webkitClipPath === 'none' ? '' : computed.webkitClipPath;
    image.style.transform = computed.transform === 'none' ? '' : computed.transform;
    image.style.transformOrigin = computed.transformOrigin;
    image.style.filter = computed.filter === 'none' ? '' : computed.filter;
    image.style.opacity = computed.opacity;
    return image;
  } catch (_) {
    return null;
  }
}

function stripIds(node) {
  if (node.removeAttribute) node.removeAttribute('id');
  for (const child of Array.from(node.children || [])) stripIds(child);
}

function createEmotionPart(part, index) {
  const image = document.createElement('img');
  image.className = 'scene-character-emotion-part';
  image.alt = '';
  image.dataset.partIndex = String(index);
  image.dataset.src = `${CHARA_EMOTION_ROOT}${part.file}`;
  image.src = image.dataset.src;
  return image;
}

function applyEmotionPartLayout(image, part, unitScale) {
  const position = part?.position || {};
  const size = part?.size || {};
  const pivot = part?.pivot || {};
  const scale = part?.localScale || {};
  const x = Number(position.x) || 0;
  const y = Number(position.y) || 0;
  const width = Math.max(1, Number(size.x) || Number(part?.naturalWidth) || 150);
  const height = Math.max(1, Number(size.y) || Number(part?.naturalHeight) || 150);
  const pivotX = Number.isFinite(Number(pivot.x)) ? Number(pivot.x) : 0.5;
  const pivotY = Number.isFinite(Number(pivot.y)) ? Number(pivot.y) : 0.5;
  const scaleX = Number.isFinite(Number(scale.x)) ? Number(scale.x) : 1;
  const scaleY = Number.isFinite(Number(scale.y)) ? Number(scale.y) : 1;
  image.style.left = `calc(50% + ${x * unitScale}px)`;
  image.style.top = `calc(50% + ${-y * unitScale}px)`;
  image.style.width = `${width * unitScale}px`;
  image.style.height = `${height * unitScale}px`;
  image.style.transform = `translate(${-pivotX * 100}%, ${-(1 - pivotY) * 100}%) scale(${scaleX}, ${scaleY})`;
}

function emotionDurationSeconds(emotion) {
  const asset = app.emotionAssets.get(normalizeAssetKey(emotion));
  return Math.max(0.1, Number(asset?.duration) || 1);
}

function cssColor(value) {
  const text = String(value || 'black').toLowerCase();
  const named = { black: '#000', white: '#fff', clear: 'rgba(0,0,0,0)', transparent: 'rgba(0,0,0,0)' };
  return named[text] || value;
}

function normalizeKey(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function normalizeMosaicMode(value) {
  const mode = normalizeKey(value || 'original');
  if (mode === 'off' || mode === 'none') return 'off';
  if (mode === 'mosaic' || mode === 'normal') return 'mosaic';
  if (mode === 'inverted' || mode === 'mosaicinsted' || mode === 'mosaicinstead') return 'inverted';
  return 'original';
}

function mosaicModeLabel(value) {
  const mode = normalizeMosaicMode(value);
  if (mode === 'mosaic') return 'Mosaic_';
  if (mode === 'inverted') return 'MosaicInsted_';
  if (mode === 'off') return 'off';
  return 'Original';
}

function live2dGroupPriority(group) {
  const index = L2D_GROUP_PRIORITY.indexOf(String(group || ''));
  return index >= 0 ? index : L2D_GROUP_PRIORITY.length;
}

function extractLive2DVoiceSuffix(voiceId) {
  const text = String(voiceId || '');
  if (!text) return '';
  const suffix = text.slice(text.lastIndexOf('_') + 1);
  return L2D_LIP_SYNC.suffixPattern.test(suffix) ? suffix : '';
}

function fileBase(path) {
  return String(path || '').split(/[\\/]/).pop().replace(/\.motion3\.json$/i, '');
}

function isNumericText(value) {
  return /^[-+]?(?:\d+\.?\d*|\.\d+)$/.test(String(value || '').trim());
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function lerp(start, end, t) {
  return start + (end - start) * t;
}

function smoothStep(t) {
  const x = clamp(t, 0, 1);
  return x * x * (3 - 2 * x);
}


function initVolumeSliderControl() {
  window.app = window.app || {};
  window.app.config = window.app.config || {};
  const bgmSlider = document.getElementById('vol-bgm');
  const seSlider = document.getElementById('vol-se');
  const voiceSlider = document.getElementById('vol-voice');
  if (!bgmSlider && !seSlider && !voiceSlider) {
    return false; 
  }
  window.app.config.bgmVolume = bgmSlider ? parseFloat(bgmSlider.value) : 1.0;
  window.app.config.seVolume = seSlider ? parseFloat(seSlider.value) : 1.0;
  window.app.config.voiceVolume = voiceSlider ? parseFloat(voiceSlider.value) : 1.0;
  if (bgmSlider) document.getElementById('vol-bgm-txt').innerText = Math.round(bgmSlider.value * 100) + '%';
  if (seSlider) document.getElementById('vol-se-txt').innerText = Math.round(seSlider.value * 100) + '%';
  if (voiceSlider) document.getElementById('vol-voice-txt').innerText = Math.round(voiceSlider.value * 100) + '%';
  function applyLiveVolume(type, currentFactor) {    
    app.controller?.sound?.setLiveVolume(type, currentFactor);
  }
  if (bgmSlider && !bgmSlider._hasInit) {
    bgmSlider._hasInit = true;
    bgmSlider.addEventListener('input', (e) => {
      const val = parseFloat(e.target.value);
      window.app.config.bgmVolume = val;
      document.getElementById('vol-bgm-txt').innerText = Math.round(val * 100) + '%';
      applyLiveVolume('bgm', val);
    });
  }
  if (seSlider && !seSlider._hasInit) {
    seSlider._hasInit = true;
    seSlider.addEventListener('input', (e) => {
      const val = parseFloat(e.target.value);
      window.app.config.seVolume = val;
      document.getElementById('vol-se-txt').innerText = Math.round(val * 100) + '%';
      
      applyLiveVolume('se', val);
    });
  }
  if (voiceSlider && !voiceSlider._hasInit) {
    voiceSlider._hasInit = true;
    voiceSlider.addEventListener('input', (e) => {
      const val = parseFloat(e.target.value);
      window.app.config.voiceVolume = val;
      document.getElementById('vol-voice-txt').innerText = Math.round(val * 100) + '%';
      
      applyLiveVolume('voice', val);
    });
  }

  return true;
}


initVolumeSliderControl();
const volInitTimer = setInterval(() => {
  if (initVolumeSliderControl()) clearInterval(volInitTimer);
}, 500);

document.addEventListener('DOMContentLoaded', () => {
  const inspectPane = document.getElementById('inspectPane');
  const toggleBtn = document.getElementById('toggleInspectBtn');
  
  if (inspectPane && toggleBtn) {
    toggleBtn.addEventListener('click', (e) => {
      e.stopPropagation(); 
      inspectPane.classList.toggle('collapsed');
      if (inspectPane.classList.contains('collapsed')) {
        toggleBtn.innerText = '<'; 
      } else {
        toggleBtn.innerText = '>'; 
      }
      const fitBtn = document.getElementById('fitBtn');
      if (fitBtn) {
        fitBtn.click(); 
      }
      window.dispatchEvent(new Event('resize'));
    });
  }
});

