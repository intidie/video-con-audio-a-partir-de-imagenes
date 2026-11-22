// Ejecuta mespeak-core.js (eSpeak en JavaScript) en Node, sin navegador.
// Uso: node mespeak_node.js entrada.json   ->  escribe un WAV por frase en "outdir".
// entrada.json: {"voice":"es","outdir":"...","items":["hola","mundo"],
//                "speed":175,"pitch":50,"amplitude":100,"wordgap":0,"variant":"m1"}
const fs = require("fs"), path = require("path"), vm = require("vm");
const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const dir = __dirname;

const sandbox = { console, setTimeout, clearTimeout, Math, Date, Uint8Array, Int8Array, Int16Array, Int32Array,
  Uint16Array, Uint32Array, Float32Array, Float64Array, ArrayBuffer, DataView, String, Array, Object, JSON };
sandbox.self = sandbox;
sandbox.location = sandbox.self.location = { href: "file:///" };
sandbox.XMLHttpRequest = function () {            // XHR mínimo: lee archivos locales
  this.open = (_m, url) => { this.url = url; };
  this.overrideMimeType = () => {};
  this.send = () => {
    try { this.responseText = fs.readFileSync(path.resolve(dir, this.url), "utf8"); this.status = 200; }
    catch (e) { this.responseText = ""; this.status = 404; }
    this.readyState = 4;
    setImmediate(() => this.onreadystatechange && this.onreadystatechange());
  };
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(dir, "mespeak-core.js"), "utf8") + "\n;this.meSpeakCore=meSpeakCore;", sandbox);
const core = sandbox.meSpeakCore;

core.loadVoice(`voices/${cfg.voice}.json`, (ok, id) => {
  if (!ok) { console.error("No se pudo cargar la voz " + cfg.voice); process.exit(2); }
  fs.mkdirSync(cfg.outdir, { recursive: true });
  const v = cfg.variant ? `${id}+${cfg.variant}` : id;
  cfg.items.forEach((text, i) => {
    const args = ["-w", "wav.wav", "-a", String(cfg.amplitude ?? 100), "-g", String(cfg.wordgap ?? 0),
      "-p", String(cfg.pitch ?? 50), "-s", String(cfg.speed ?? 175), "-b", "1", "-v", v,
      "--path=/espeak", String(text)];
    const wav = core.speak(args);
    fs.writeFileSync(path.join(cfg.outdir, `w${String(i).padStart(5, "0")}.wav`), Buffer.from(new Uint8Array(wav)));
  });
  console.log("ok " + cfg.items.length);
});
