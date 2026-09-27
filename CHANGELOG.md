# Ändringslogg

Varje version av **tjänsten** och **integrationen**, vad som ändrades och varför. Versionen
står i `config.py` (`VERSION`) och visas i gränssnittets överkant; HACS visar samma nummer för
integrationen (`custom_components/vatten_kamera/manifest.json`). Testerna ser till att de två
aldrig glider ifrån varandra.

Filen finns **för att ändringarna inte ska leva bara i en chatt**: en lång konversation
komprimeras till en sammanfattning, och då är detaljerna borta. Här ligger de kvar,
versionsvis, och följer med i varje `git pull`.

Nyast överst. Datum är svenska.

---

## 0.20.0 — 2026-09-27

**Tjänsten kraschade i en evig omstartsslinga så fort ingen sida gick att läsa (minnet tog slut).**

* Kameran hade flyttat sig några pixlar — siffrorna satt 20 px längre åt höger och rörde vid
  bildens överkant. Mätfönstren i `calibration.json` pekade då fel, och **ingen sida gick att
  läsa**. Det syns inte som ett fel: körningen letar vidare under hela fönstret.
* Under tiden sparades **varje bild i minnet** (utsnitt + JPEG, ~250 kB per bild). Efter 30
  minuter (`WINDOW_S`) hade ~300 MB samlats i en LXC med 512 MB → **OOM-killern dödade tjänsten
  mitt i körningen**, systemd startade om den var ~31:a minut, och samma sak hände igen. I
  loggen syntes bara `startar korning` — ingen `klar`-rad, inget värde, ingen förklaring.
* Körningen håller nu bara de **sista 200 bilderna** i minnet (`MAX_BUFFERED_FRAMES`). Det
  räcker: en sida står i 10–12 s, displayens varv är ~1 minut och värdet letas upp mot slutet
  av körningen. Minnet är begränsat till ~50 MB hur länge körningen än letar.
* Nytt test: en körning som aldrig hittar värdet får inte samla fler bilder än taket.
* `calibration.json` mättes om mot kamerans nya läge (cellrutorna flyttades +20 px i x och
  några px i y). Filen är platsspecifik och ligger inte i repot.
* Verifierat live efter fixen: värdet **1.34** (konfidens 0.91) och flödet läses som förut.

## 0.19.0 — 2026-09-26

**Kameran i Home Assistant visar bilden igen.**

* Entiteten `camera.vatten_kamera_senaste_bild` ("Last image") skapades men svarade aldrig med
  någon bild. Orsaken var klassordningen: `SenasteBildCamera` ärver `VattenKameraEntity`
  **före** HA:s `Camera`, och HA:s `BaseCoordinatorEntity.__init__` anropar **inte**
  `super().__init__()`. Då körs aldrig `Camera.__init__`, som sätter upp `_cache` och
  `access_tokens` — och HA:s cachade egenskaper (`is_on`, `is_streaming`, `entity_picture`)
  kastar då `AttributeError`, så ingen bild kan hämtas.
* Fixen är den som HA:s egna integrationer använder när de blandar `CoordinatorEntity` med
  `Camera`: `Camera.__init__(self)` anropas uttryckligen i `__init__`.
* Nytt test (`tests/test_hacs.py`) läser `camera.py` som ett syntaxträd och kräver att
  anropet finns kvar.

## 0.18.0 — 2026-09-26

**Läckagelarmet kräver ett oavbrutet uttag i 30 minuter (och kunde tidigare aldrig tändas).**

* Larmvillkoret krävde att den **äldsta** läsningen i 30-minutersfönstret var minst 24 minuter
  gammal. Tjänsten läser var tionde minut, så den äldsta läsningen i fönstret är som mest
  ~20 minuter — **larmet kunde alltså aldrig tändas**, och hade det tänts hade det slocknat
  vid nästa läsning. Mätt mot simulerad historik i tjänstens takt.
* Nu mäts längden som en **obruten följd av läsningar över tröskeln**, i *tid* i stället för i
  antal: 30 minuter är tre läsningar var tionde minut eller femton varannan. Larmet ligger
  kvar så länge det rinner och slocknar vid första läsningen med stilla vatten.
* **Ett badkar, en dusch eller en tvätt larmar inte** — det är korta uttag. Ett oavbrutet
  uttag i 30 minuter gör det.
* Två spärrar: ett glapp mellan läsningarna som är längre än kravet gör att följden inte
  bedöms alls (t.ex. efter ett avbrott), och en läsning **utan** känt flöde larmar aldrig.
* Tester: 201 (fyra nya för takten, badkaret, släckningen och glappet).

## 0.17.0 — 2026-09-26

**Flödet läses i samma körning som värdet — och ett läckagelarm.**

* Displayens varv är `klocka → spolttid 02:00 → VÄRDET → FLÖDET`. Värdet och flödet ser
  **likadana** ut för avläsaren; bara ordningen skiljer dem. Körningen läser därför värdet som
  förut och tar flödet på sidan efter, och stannar först när båda är fångade
  (`FLOW_EXTRA_S`, körningen tar ~20 sekunder längre).
* Nya inställningar: `READ_FLOW`, `FLOW_EXTRA_S`, `FLOW_WARN`, `FLOW_WARN_MINUTES`,
  `FLOW_UNIT`. Flödet hamnar i `latest.json`, i historiken, som streckad linje i grafen och i
  Home Assistant (`sensor.vatten_kamera_flode`, `binary_sensor.vatten_kamera_lackage`).
* Flödesenheten `l/h` är en **gissning** som ska kontrolleras mot pumpen.

## 0.16.0 — 2026-09-26

* Containern ärvde UTC medan Proxmox-hosten kör `Europe/Stockholm` → tiderna blev två timmar
  fel och en nattkörning hade startat fel. Tidszonen följer nu med från hosten
  (`--timezone` i installationsskripten).
* Länk som lägger till repot i HACS med ett klick (README).

## 0.15.0 — 2026-09-26

* **Tjänsten dör inte längre utan kalibrering.** Saknades `calibration.json` kastade
  `NightlyRunner.__init__` ett fel, hela processen (och därmed gränssnittet) dog och systemd
  startade om i evig loop — felet syntes bara i journalen. Nu blir kalibreringen `None`,
  körningen hoppas över, `/api/health` svarar `kalibrering: false`, gränssnittet visar vad som
  saknas och `reload()` plockar upp filen utan omstart.

## 0.14.2 — 2026-09-26

* Tomma värden följer inte med i inställningsfilen — annars kunde `--unit l` från
  installationen raderas av en tom rad.

## 0.14.1 — 2026-09-26

* Installationsskripten kontrollerar att filerna finns **innan** containern skapas: en
  felstavad sökväg lämnar inte en halvfärdig container efter sig.

## 0.14.0 — 2026-09-26

* Filerna kopieras med `pct push` i stället för `scp` (en ny container har ingen SSH-server).
* `proxmox-create.sh --settings`: containern får med sig `.env`, kalibreringen, läsprofilen och
  kamerabackupen i samma kommando.

## 0.13.0 — 2026-09-26

* **`tools/settings_file.py`**: flyttar hela intrimningen (`.env`, `calibration.json`,
  `camera_profile.json`, `camera_settings_backup.json`) till en annan maskin — **utan**
  lösenord och tokens (allt som slutar på `_PASSWORD`/`_TOKEN` filtreras bort).

## 0.12.3 — 2026-09-26

* Installationen: locale-varningarna tystade, rätt schema i utskriften (inget "Klockslag
  02:05" i intervall-läget), frågar efter kamerans användare och säger att den tar några
  minuter.

## 0.12.2 — 2026-09-26

* **Brand-ikonen** (`brand/icon.png`) med i git — annars saknas den när HACS installerar.
* README: guiden **"Lägga in kameran"**, kommandona inuti containern (`pct enter`,
  `install.sh`, drift), `curl` i en helt ny container, och vad repot måste uppfylla innan
  HACS kan använda det.
* **Repot gjordes publikt** (HACS kan inte läsa privata repon) och historiken skrevs om så att
  kamerans adress och kalibreringen är borta ur *all* historik.

## 0.12.1 — 2026-09-26

* Installationen i LXC: rätt sökväg för kalibreringen, kalibreringen kan följa med in, och ett
  dubbelt fält bort ur gränssnittet.

## 0.12.0 — 2026-09-21

* **Bara bevisbilden sparas** (en per läsning, ~110 kB) i stället för en bild per läsbar sida —
  från ~180 MB/dygn till ~16 MB/dygn. `tools/daily_report.py` ger en stabilitetsrapport.
* Ändrat läsintervall slår igenom **direkt**: nästa tid räknas om medan tjänsten väntar.
* **Kameran sköts i gränssnittet** (läget just nu, läsprofil, backup, återställ) och inga
  kamerauppgifter ligger i repot.

## 0.11.0 — 2026-09-21

* **Graf över värdet** där man klickar på en punkt och ser bevisbilden värdet lästes ur.
  Bilderna städas automatiskt och sparas i 7 dygn.

## 0.10.0 — 2026-09-21

* **Tre lägen**: `intervall` (hela tiden, standard), `manuell` (bara på begäran) och `natt`.
* **HACS-integrationen**: `sensor.vatten_kamera_niva`, `..._senast_last`, `..._status`,
  `binary_sensor..._lasning_ok`, `button..._las_nu` och `camera..._senaste_bild`. Ingen
  bildbehandling i HA och inga extra paket.

## 0.9.0 — 2026-09-21

* Värdet läses rätt: mätfönstren mättes om mot **riktiga bilder** i stället för ett idealt
  sjusegment (displayens fyra har stapeln ända upp, sexan/femman har en hake, översta strecket
  lutar ner i `f`). 80 av 80 siffror rätt med `tools/check_digits.py`.
* Röstdningen följer sidan efter `02:00`; en oläsbar sida på många bilder publicerar **inget**
  (flödessidan `0.00` ser likadan ut och får aldrig bli värdet).

## 0.7.0 — 2026-09-20

* **Kameran ser panelen snett** → varje sifferposition mäts för sig (x och y), inte alla på
  samma rad.
* **Exponeringen är kritisk** (röd LED är svag i blåkanalen): gain och slutare mäts med
  `main.py image tune`; kameran tippas så översta siffran inte klipps.
* Tiden som gäller är **pumpens** klocka — den går ~5 minuter efter, därför `RUN_AT`.
* Nian har ett **kort** bottenstreck och ettans stapel lyser i samma fönster → fönstren är
  avvägda mot den här displayens typsnitt, inte mot ett idealt sjusegment.

## 0.6.1 — 2026-09-18

* Mätfönstren tål glöden från smala ettor — värdet läses nu.

## 0.6.0 — 2026-09-18

* Varje bildruta riktas in mot en referensbild.

## 0.5.x — 2026-09-18

* 0.5.3: krav på **släckt första position** skiljer värdet från klockan och spolttiden.
* 0.5.2: jämn delning av sifferbandet och uppmätta sifferfönster.
* 0.5.1: mittensegmentet läses inte längre som tillslaget i en nolla.
* 0.5.0: fyra siffror i displayen, bredare ROI och bredare segmentfönster.

## 0.4.0 — 2026-09-18

* Siffrorna hittas som **klumpar** och displayen mäts med siffror i stället för ögonmått.
* **Kameran lånas bara under läsningen** och lämnas aldrig i det mörka läget (hände en gång —
  användaren såg en helt svart bild).
* Kamerans backup och läsprofil är lokal konfiguration, inte kod.

## 0.3.0 — 2026-09-18

* Värdeformatet är `d.dd`: två decimaler, `122` är alltså `1.22` — och alla siffror måste
  lysa för att värdet ska godkännas.

## 0.2.0 — 2026-09-18

* Utsnittet dras åt automatiskt och ett diagnoskommando (`main.py diagnose`) tillkom.
* Kalibreringen är platsspecifik och versionshanteras inte.

## 0.1.0 — 2026-09-18

* Första versionen: läser pumpdisplayen efter 02:00 och skickar värdet till Home Assistant.
