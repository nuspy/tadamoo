> Riferimento: [README](../../../README.it.md).

## Misure della comunità

Righe inviate al progetto originale (`docs/community/benchmarks.json` di `jesdga95/basalt`, copiate il 2026-10-26, 25 prove), tutte prese con
`basalt-bench --community`, quindi confrontabili tra loro: velocità di lettura di un prompt da 16k token (token/s) e velocità di scrittura
(token/s), ciascuna con il limite di potenza della GPU con cui è stata fatta. Il merito è di chi le ha inviate. Le due righe in fondo sono
**le nostre misure di sviluppo** su questo fork (Windows, RTX 5090, 600 W, 127,6 GiB di RAM) e **non usano lo stesso metodo**: la scrittura è
una richiesta di chat con temperatura 0,6 e il drafter acceso, la lettura è un testo ripetuto da 13,5k token, e il modello è la versione
Uncensored di OrcaRouter della stessa architettura. Le righe della stessa fonte si confrontano tra loro; queste due con le altre solo come
ordine di grandezza.

| GPU | Potenza | RAM GiB | SO | Quant | Prompt 16k tok/s | Scrittura tok/s | Inviata da |
|---|---|---|---|---|---|---|---|
| 2x RTX 5090 32 GB | 500 W | 123 | Linux | NVFP4 | 7.740 | 461 | neuro |
| RTX 5060 Ti + RTX 5090 32 GB | 400 W | 121 | Linux | NVFP4 | 7.741 | 438 | jesdga95 |
| RTX 5090 32 GB | 575 W | 62 | Linux | IQ3_S | 5.677 | 376 | knoopx |
| RTX 5090 32 GB | 500 W | 62 | Linux | IQ3_S | 8.859 | 375 | drakiss79 |
| RTX 5090 32 GB | 500 W | 62 | Linux | IQ3_XXS | 8.784 | 564 | drakiss79 |
| RTX 5090 32 GB | 475 W | 247 | Linux | NVFP4 | 9.556 | 312 | giveen |
| RTX 5090 32 GB | 500 W | 62 | Linux | NVFP4 | 9.289 | 305 | drakiss79 |
| RTX 5090 32 GB | 500 W | 191 | Windows | NVFP4 | 2.431 | 199 | HenryPDT |
| RTX 5090 32 GB | 400 W | 121 | Linux | NVFP4 | 7.732 | 317 | jesdga95 |
| RTX 5090 Laptop GPU 24 GB | 175 W | 47 | WSL2 | IQ3_XXS | 3.892 | 243 | pyj99_fr |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | IQ3_S | 3.226 | 187 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | IQ3_S | 3.236 | 158 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | IQ3_XXS | 3.169 | 202 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | IQ3_XXS | 2.894 | 189 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | NVFP4 | 4.072 | 131 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | NVFP4 | 3.316 | 96 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | Q4_K_XL | 2.983 | 72 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q4_K_XL | 2.866 | 104 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q5_K_XL | 2.981 | 77 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q6_K_XL | 2.674 | 66 | foolio |
| 2x RTX 5060 Ti 16 GB | 180 W | 60 | Linux | IQ3_S | 2.370 | 161 | KimDaeHwan99 |
| RTX 5060 Ti 16 GB | 180 W | 121 | Linux | IQ3_S | 2.427 | 137 | jesdga95 |
| RTX 5060 Ti 16 GB | 180 W | 121 | Linux | NVFP4 | 2.687 | 92 | jesdga95 |
| RTX 5080 16 GB | 360 W | 88 | WSL2 | IQ3_S | 3.180 | 138 | lindworm-hermes2026 |
| RTX 5080 16 GB | 360 W | 88 | WSL2 | Q4_K_XL | 2.835 | 78 | lindworm-hermes2026 |
| RTX 5090 32 GB | 600 W | 127,6 | Windows | IQ3_XXS (questo fork, metodo nostro) | 7.319 | 179 (esatta) / 195 (rilassata 0,3) | questo fork |
| RTX 5090 32 GB | 600 W | 127,6 | Windows | IQ4_XS (questo fork, metodo nostro) | 5.206 | 100 (esatta) / 117 (rilassata 0,3) | questo fork |


## Licenza

Il codice nuovo e modificato di questo fork è rilasciato sotto la **TaDaMoo Win+GUI Community and Commercial License** (vedi [LICENSE](../../../LICENSE)): uso gratuito per privati e per aziende con fatturato lordo annuo consolidato inferiore a 10 milioni di dollari USD (licenza commerciale enterprise richiesta a partire da $10M). Il codice preesistente upstream (Strata, Basalt originale, ggml) rimane sotto le rispettive licenze MIT (vedi [THIRD_PARTY_LICENSES](../../../THIRD_PARTY_LICENSES)).
