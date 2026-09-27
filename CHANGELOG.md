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

## 0.26.0 — 2026-09-27

**Utsnittet växer bara när det behövs — och notisen säger vad som gjordes.**

* `CALIBRATION_ROI` behandlades som "minst 48 px luft runt rutorna", så den växte några pixlar varje
  gång man tryckte Spara (första gången: 985 → 981). Nu vidgas den **bara** när en ruta hamnar
  utanför utsnittet — annars står den still. Den krymps fortfarande aldrig.
* Notisen i `calibration.json` skiljer på "mätt fram i gränssnittet" och "flyttat i gränssnittet",
  så filen berättar vad som gjordes.
* Nytt test: att utsnittet står still när rutorna ryms.

## 0.25.0 — 2026-09-27

**Kalibreringspanelen skiljer på värde och tidssida — och säger vad som är fel.**

* Displayen visar fyra slags sidor: klockan, spolttiden `02:00`, värdet och flödet. På en tidssida
  lyser **alla fyra** positionerna, och då är läsningens konfidens 0 (på en värdesida ska första
  positionen vara släckt). Panelen skrev därför "rutorna pekar fel" trots att siffrorna lästes med
  konfidens 0.90-0.98.
* Svaret skiljer nu på `sida: värde | tid | okänt`. En tidssida visas **dämpad** med texten
  "siffrorna läses säkert - displayen visar en tidssida (13.36). Ta 'Ny bild' för att se en
  värdesida." — rutorna sitter ju rätt, det finns bara inget värde på den sidan.
* Är en siffra svag står det **vilken** och hur svag: "position 3 läser '3' med konfidens 0.13 —
  flytta den rutan" i stället för ett allmänt felmeddelande.
* Nytt test för just tidssidan.

## 0.24.0 — 2026-09-27

**Kalibreringen tar kameran för sig själv — och en läsning som letar förgäves släpper.**

* Kameran (2014 års modell) svarar bara **en klient i taget**. När en läsning pågick samtidigt som
  man kalibrerade svarade den `Connection aborted`, och båda tog dubbelt så lång tid: "Mät
  automatiskt" kunde hålla på i minuter i stället för en halv minut.
* Ny avbrottsflagga i `pipeline.py` (`begar_avbrott()`): bildslingan tittar efter den mellan
  bilderna och avslutar körningen snällt. Gränssnittet ber om avbrott och väntar (upp till 30 s) på
  att kameran blir ledig innan det tar sina egna bilder. Flaggan nollställs när en ny körning börjar.
* En körning som avbryts skriver sin rad i historiken som vanligt (inget värde den gången) — den
  läste ju inget, och det ska synas.
* Två nya tester: att slingan släpper kameran på begäran, och att väntan ger upp när tiden är slut.

## 0.23.0 — 2026-09-27

**Alla paneler går att stänga — och är stängda när man kommer in.**

* Värdets utveckling, Rutan som ser siffrorna, Status och tester, Inställningar och Logg ligger nu i
  hopfällbara paneler (webbläsarens egna `details`/`summary`, ingen extern kod). Bara värdet med
  bevisbilden är öppet när sidan laddas — öppnar man en panel är den öppen tills man laddar om.
* Panelen **Rutan som ser siffrorna** ritar om bilden när den öppnas (en stängd panel har ingen
  bredd, så rutorna skulle annars hamna snett), liksom grafen.
* Varningen för saknad kalibrering pekar nu på panelen och **Mät automatiskt** i stället för på
  kommandoraden.

## 0.22.0 — 2026-09-27

**Rutan som ser siffrorna kan flyttas i gränssnittet.**

* Kameran sitter i en källare och får en knuff när saltet fylls på. Då pekar rutnätet
  (`cell_boxes` i `calibration.json`) fel och inget värde publiceras — och att rätta till det krävde
  att man mätte på en annan maskin och kopierade in filen i containern.
* Ny panel **"Rutan som ser siffrorna"**: bilden av displayen med de fyra rutorna ovanpå. Bilden är
  en **tidsstack** (den ljusaste pixeln av flera bilder), så att en siffra syns i varje position —
  på värdesidan är första positionen ju släckt.
* **Dra i en ruta** (eller markera den och använd piltangenterna; skift tar tio steg) och tryck
  **Spara & läs**. Rutan blir grön, gul eller röd beroende på vad siffran under den läser
  (säker / osäker / går inte att läsa), och panelen visar varje positions tecken och konfidens.
  "Flytta alla fyra samtidigt" är på som standard — kameran rubbas ju som en helhet.
* **Mät automatiskt** mäter fram rutnätet i färska bilder (samma väg som `main.py calibrate`) och
  sparar det. **Ny bild** tar en ny tidsstack, **Ångra** läser om det sparade.
* Hamnar en ruta utanför utsnittet (`CALIBRATION_ROI`) vidgas utsnittet automatiskt vid sparandet —
  annars klipps siffran bort innan rutan ens får se den — och `CALIBRATION_ROI` skrivs också till
  `.env`, eftersom verktygen mäter mot den.
* Nya vägar i API:t: `GET /api/calibration`, `GET /api/calibration/bild`,
  `POST /api/calibration` (spara), `POST /api/calibration/ny` och `POST /api/calibration/mat`.
  De skriver till `calibration.json` — samma fil som verktygen — så nästa läsning använder de nya
  rutorna utan omstart.
* Sex nya tester mot syntetiska bilder (ingen kamera inblandad).

## 0.21.0 — 2026-09-27

**Ett värde med en tvåa i sista siffran kunde inte publiceras alls.**

* Displayens tvåa har sitt översta streck förskjutet åt höger: det börjar först vid x 0.36 i cellen,
  medan en nolla och en trea börjar redan vid x 0.16-0.34. Mätfönstret för översta strecket låg på
  x 0.18-0.45 och täckte då bara ~33 % av tvåans streck → 75:e percentilen hamnade på den mörka
  delen, tvåan mättes `a=0.26` i stället för 1.0 och fick konfidens **0.34** — precis under
  `MIN_CONFIDENCE` (0.35). Värdet `1.32` lästes alltså rätt men publicerades aldrig, och flödet
  kunde inte heller användas (inget värde att jämföra mot).
* Fönstret ligger nu på x 0.34-0.48 — mitt i cellen, där tvåans, fyran och treans streck finns.
  Det får inte gå längre åt höger: där börjar ettans stapel (x 0.68 på den verkliga displayen,
  x 0.52 i testritarens geometri) och tänds fönstret läses ettan som en sjua.
* Mätt med `tools/probe_cell.py` mot den nya bildserien, och verifierat att `tools/check_digits.py`
  fortfarande ger **80 av 80** kända siffror rätt, ingen under 0.5.
* Efter fixen läses tvåan med konfidens **0.70-0.79** (var 0.34) och ettan ligger kvar på 0.83-0.87.

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
