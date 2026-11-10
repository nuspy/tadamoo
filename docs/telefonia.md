# Voce e telefonate in cloud: confronto fornitori

Per chi usa TaDaMoo su un server remoto senza scheda video adatta: STT, TTS e linea telefonica come servizi separati e intercambiabili. Prezzi in dollari, letti il 10 novembre 2026. I fornitori li cambiano spesso: rileggere la pagina ufficiale prima di fissare un budget.

Fonti: dove non è scritto altro, la pagina ufficiale del fornitore. «(non verificato)» = fonte di terzi o pagina ufficiale senza cifre leggibili.

## Cosa manca nel codice
- Le telefonate usano solo due trasporti: `gsm` (modem USB) e `android` (telefono via Bluetooth). Un server remoto non ha né l'uno né l'altro.
- Mancano i motori cloud per STT e TTS (oggi solo Kokoro, Piper, XTTS e Whisper in locale).
- Manca un trasporto SIP (vedi sotto) verso un fornitore di linea.
- L'avatar (MuseTalk) richiede comunque una scheda video.

## SIP trunking in due righe
SIP è il protocollo con cui un programma avvia, risponde e chiude una telefonata su Internet. Il trunk è il collegamento tra il programma e la rete telefonica pubblica (fissi e cellulari). Il fornitore dà una linea virtuale: niente modem né SIM, i canali sono quanti si pagano.

## 1. Riconoscimento vocale (STT)

| Fornitore | Prezzo | Lingue | Regioni e dati |
|---|---|---|---|
| [Deepgram](https://deepgram.com/pricing) Nova-3 | $0,0092/min in streaming, $0,0052/min su registrato; $200 di credito iniziale | 60+ | endpoint UE (`api.eu.deepgram.com`), [disponibile da gennaio 2026](https://deepgram.com/learn/deepgram-eu-endpoint-now-generally-available), senza sovrapprezzo |
| [ElevenLabs](https://elevenlabs.io/pricing/api) Scribe v2/v3 | $0,20/ora (circa $0,0033/min) | 90+ | dati negli USA di default; residenza UE solo Enterprise (non verificato) |
| [OpenAI](https://developers.openai.com/api/docs/pricing) | gpt-4o-mini-transcribe $0,003/min; gpt-4o-transcribe e whisper $0,006/min | non indicate | non trovato |
| [AWS Transcribe](https://aws.amazon.com/transcribe/pricing/) | $0,01/min in streaming, $0,006/min su registrato (US East); 60 min gratis al mese per 12 mesi | non indicate | molte regioni, prezzi diversi per regione |
| Google STT V2 | $0,016/min fino a 500.000 min al mese; dynamic batch circa $0,003 ([fonte di terzi](https://diyai.io/ai-tools/speech-to-text/google-cloud-speech-to-text-pricing/), non verificato) | non verificate | regioni Google Cloud |
| Azure Speech | circa $0,18/ora in batch, circa $0,017/min in tempo reale (non verificato); 5 ore gratis al mese | non indicate | circa 61 regioni |

## 2. Sintesi vocale (TTS)

| Fornitore | Per 1 milione di caratteri | Lingue | Note |
|---|---|---|---|
| AWS Polly | $4 Standard, $16 Neural, $30 Generative | non indicate | 5 M di caratteri Standard gratis al mese |
| [Google](https://cloud.google.com/text-to-speech/pricing) | Standard circa $4 (non verificato); Chirp 3 HD $30 | non verificate | 1 M gratis al mese; solo Chirp 3 HD confermato da più pagine |
| Azure Neural | circa $15–16; HD circa $22 (non verificato) | non verificate | 0,5 M gratis al mese |
| OpenAI | tts-1 $15; tts-1-hd $30; gpt-4o-mini-tts a token ($12 per milione di token audio) | non indicate | |
| Deepgram Aura-2 | $30 | non indicate | endpoint UE |
| ElevenLabs | Flash/Turbo $40 (32 lingue); v2 $80 (29); v3 $80 (70+) | fino a 70+ | qualità più alta, costo più alto |

## 3. Linea telefonica

Tariffe base, USA:

| Fornitore | Al minuto | Numero al mese | Note |
|---|---|---|---|
| [Twilio](https://www.twilio.com/en-us/voice/pricing/us) | $0,014 in uscita, $0,0085 in entrata | $1,15 | Media Streams $0,0044/min; ConversationRelay $0,07/min; copertura più ampia |
| [Telnyx](https://telnyx.com/pricing/call-control) | Voice API $0,002 più SIP $0,005 in uscita e $0,0032 in entrata | $1,00 | tariffe per paese solo nel listino scaricabile |
| [Plivo](https://www.plivo.com/voice/pricing/) | $0,0115 in uscita, $0,0055 in entrata; browser e SIP $0,0033 | $0,50 | la più economica tra quelle verificate |
| SignalWire | $0,008 in uscita, SIP $0,003 | $0,50 | solo fonti di terzi (non verificato) |
| Vonage | nessun listino pubblico trovato | | tariffe visibili solo nel pannello; il listino cambia ogni 4 ore |

### Uscita verso altri paesi (al minuto, fisso / cellulare)

Twilio dal [listino CSV](https://www.twilio.com/content/dam/twilio-com/pricing-data/en/csv/PMded94a0dae30eaaec0f115f22859bd38_OutboundVoicePricing.csv); Plivo dalle pagine [Italia](https://www.plivo.com/sip-trunking/pricing/it/), [Germania](https://www.plivo.com/sip-trunking/pricing/de/), [Regno Unito](https://www.plivo.com/sip-trunking/pricing/gb/).

| Paese | Twilio | Plivo |
|---|---|---|
| Italia | 0,0168 / 0,33–0,35 | 0,0100 / 0,45 |
| Germania | 0,0150 / 0,35 | 0,0096 / 0,382 |
| Regno Unito | 0,0150 / 0,029 | 0,0072 / 0,0171 sulle reti principali |
| Francia | 0,0147 / 0,151 | non letto |
| Spagna | 0,0178 / 0,18 | non letto |
| Brasile | 0,040 / 0,062 | non letto |
| India | 0,047 / 0,037 | non letto |
| Giappone | 0,061 / 0,185 | non letto |
| Australia | 0,024 / 0,075 | non letto |

Ungheria: vedi sotto (i dati del CSV e della pagina differiscono, vale la pagina).

### Ungheria: l'origine della chiamata cambia il prezzo

| Fornitore | Fisso, origine UE/SEE | Cellulare, origine UE/SEE | Fisso, altra origine | Cellulare, altra origine | Numero al mese |
|---|---|---|---|---|---|
| [Plivo](https://www.plivo.com/voice/pricing/hu/) | 0,0530 | 0,0566 | 0,108 | 0,111–0,124 | non indicato; chiamate in entrata non supportate |
| [Twilio](https://www.twilio.com/en-us/voice/pricing/hu) | 0,0150 | 0,0630 | 0,1042 | 0,1155 | da 1,15 |
| Telnyx | non trovato | non trovato | non trovato | non trovato | |
| Vonage | non trovato | non trovato | non trovato | non trovato | |

Verso i cellulari ungheresi una chiamata con origine UE costa circa la metà. Non è verificato come i fornitori stabiliscono l'origine (regione dell'account, numero chiamante o SIP): chiarirlo prima di scegliere.

Altri costi Twilio per l'Ungheria: browser/app e SIP $0,0040/min; registrazione $0,0025/min più $0,0005/min al mese di archivio; rilevamento segreteria $0,0075 a chiamata.

### Numeri italiani
Un numero italiano richiede documenti: identità, codice fiscale e un indirizzo nel distretto del prefisso. Vale per [Twilio](https://www.twilio.com/en-us/guidelines/it/regulatory) e per [Telnyx](https://support.telnyx.com/en/articles/1311462-italy-did-requirements) (obbligo dal 16 settembre 2024). Il canone mensile per l'Italia non è stato trovato.

## 4. Costo di un'ora di chiamata

Ipotesi: STT in streaming per tutti i 60 minuti, circa 27.000 caratteri sintetizzati.

| Combinazione | Costo all'ora |
|---|---|
| Deepgram + AWS Polly Standard + Telnyx (USA) | circa $1,1 |
| Deepgram + ElevenLabs Flash + Twilio (USA) | circa $2,5 |
| Linea verso un fisso italiano (Plivo / Twilio), solo linea | circa $0,60 / $1,01 |
| Linea verso un cellulare italiano (Twilio / Plivo), solo linea | circa $21 / $27 |
| Cellulare ungherese, origine UE: Plivo + Deepgram + Polly Standard | circa $4,1 |
| Cellulare ungherese, origine UE: Twilio + Deepgram + Polly Standard | circa $4,4 |
| Cellulare ungherese, origine UE: Twilio con ConversationRelay | circa $8 (se la tariffa non comprende STT e TTS; non verificato) |

La linea verso i cellulari pesa più di STT e TTS messi insieme.

## 5. Indicazioni
- **STT:** Deepgram (streaming, endpoint UE senza sovrapprezzo, credito iniziale). Per costi bassi: ElevenLabs Scribe.
- **TTS:** AWS Polly o Google per il costo minimo; ElevenLabs per qualità e lingue (70+ con v3).
- **Linea:** Plivo o Telnyx per il costo, Twilio per copertura e documentazione. Verso cellulari italiani e ungheresi confrontare i listini prima di scegliere.
- **Fornitore unico:** Twilio ha linea, STT e TTS nello stesso account (un solo conto e una sola fattura), a costo più alto. Telnyx e Vonage hanno piattaforme voce con STT e TTS integrati, non valutate qui. Plivo ha solo la linea.
