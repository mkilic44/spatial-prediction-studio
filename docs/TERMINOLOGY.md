# Terminology: Turkish to English

Spatial Prediction Studio was first written with a Turkish user interface. This file fixes the English term for every recurring Turkish term. Use it when you change interface text, error messages, reports or documentation. One Turkish term has one English term throughout the product.

Soil science terms follow the shared soil terminology glossary of the author (version 1.2, 17 September 2026). Statistics and machine learning terms follow common usage in digital soil mapping and in scikit-learn.

## Writing rules for interface and report text

- Use sentence case for labels, buttons, tabs and headings: "Save model", not "Save Model".
- Keep messages short and literal. One idea per sentence. No sentence longer than 35 words.
- Do not use the en dash, em dash or minus sign characters (U+2013, U+2014, U+2212). Use a hyphen, comma or colon.
- Spell out an abbreviation at its first use in a report or help text: "root mean squared error (RMSE)".
- Keep machine identifiers unchanged: error codes such as `INPUT_PATH`, method IDs such as `spatial_block`, JSON keys, file names and placeholders.
- Use American spelling: neighbor, color, behavior, modeling.

## Interface structure (ArcMap panel)

| Turkish | English |
|---|---|
| Mekânsal Tahmin (toolbar and window caption) | Spatial Prediction |
| Mekânsal tahmin panelini aç | Open the spatial prediction panel |
| 1 Proje | 1 Project |
| 2 Veri ve hedef | 2 Data and target |
| 3 Doğrulama | 3 Validation |
| 4 Ön işleme | 4 Preprocessing |
| 5 Değişken seçimi | 5 Variable selection |
| 6 Model ve arama | 6 Model and search |
| 7 Değerlendirme | 7 Evaluation |
| 8 Belirsizlik | 8 Uncertainty |
| 9 Haritalama | 9 Mapping |
| 10 Rapor ve günlük | 10 Report and log |
| Modeli çalıştır | Run model |
| İptal et | Cancel |
| Katları göster | Show folds |
| Ön kontrol | Preflight check |
| Modeli kaydet | Save model |
| Seç | Select |
| Katmanları yenile | Refresh layers |
| Örneği yükle | Load example |
| Proje aç | Open project |
| Proje kaydet | Save project |
| Raporu aç | Open report |
| Çalışma paketini kaydet | Save study package |
| Tam çalışma paketini kaydet | Save full study package |
| Günlüğü kaydet | Save log |
| Geçmiş sonucu haritaya ekle | Add previous result to map |
| Ayrıntılı çalışma raporu | Detailed study report |
| Tam çalışma raporu | Full study report |
| kat ön gösterimi | fold preview |
| ön gösterim | preview |

## Runs, jobs and files

| Turkish | English | Note |
|---|---|---|
| çalışma (an executed analysis) | run | matches `run_id`, `runs/` |
| çalışma (report and export scope) | study | matches `study_report`, `study_export` |
| geçmiş çalışma / geçmiş sonuç | previous run / previous result | |
| iş | job | |
| işçi, işçi süreci | worker, worker process | |
| iş parçacığı | thread | |
| süreç kimliği | process ID | |
| günlük | log | |
| çıktı, çıktı klasörü | output, output folder | |
| üst veri | metadata | |
| dosya özeti, SHA-256 özeti | file digest, SHA-256 digest | |
| kanıt | evidence | |
| kabul sınaması | acceptance test | |
| sınama | test | |
| ön kontrol | preflight check | |
| yöntem kaydı | method registry | |
| istemci, sunucu, araç (MCP) | client, server, tool | |
| yerel | local | |

## Data and samples

| Turkish | English |
|---|---|
| gözlem | observation |
| örnek | sample |
| nokta, nokta katmanı | point, point layer |
| gözlem kimliği, kimlik | observation ID, ID |
| tekil gözlem | unique observation |
| hedef, hedef alanı, hedef birimi | target, target field, target unit |
| hedef toprak özelliği | target soil property |
| yardımcı değişken, çevresel değişken | covariate, environmental covariate |
| değişken | variable |
| raster bandı | raster band |
| sürekli, kategorik | continuous, categorical |
| koordinat sistemi | coordinate reference system (CRS) |
| koordinatlı tablo | table with coordinates |
| virgülle ayrılmış dosya | comma-separated values (CSV) file |
| X / boylam alanı, Y / enlem alanı | X / longitude field, Y / latitude field |
| derinlik üst sınırı, derinlik alt sınırı | upper depth, lower depth |
| ölçüm yöntemi, laboratuvar yöntemi | measurement method, laboratory method |
| örnekleme tarihi | sampling date |
| örnek desteği | sample support |
| kompozit örnek | composite sample |
| parsel ortalaması | parcel mean |
| örneğin temsil ettiği alan | area represented by the sample |
| profil / parsel / işletme | profile / parcel / farm |
| dışlama, dışlanan gözlem, gerekçe | exclusion, excluded observation, reason |
| eksik değer | missing value |
| sentetik | synthetic |

## Validation design

| Turkish | English |
|---|---|
| doğrulama | validation |
| değerlendirme | evaluation |
| bağımsız sınama, bağımsız test | independent test |
| kilitli bağımsız test | locked independent test set |
| tahmin amacı | prediction purpose |
| çalışma alanı içinde tahmin | prediction within the study area |
| başka alana aktarım | transfer to another area |
| kat, dış kat, iç kat | fold, outer fold, inner fold |
| kat dışı tahmin | out-of-fold prediction |
| birleştirilmiş kat dışı değerlendirme | pooled out-of-fold evaluation |
| tekrar, tekrarlı | repeat, repeated |
| tekrar bazında değerlendirme | per-repeat evaluation |
| kat ölçütlerinin ortalaması | mean of fold metrics |
| rastgele k-katlı | random k-fold |
| gruplu k-katlı | grouped k-fold |
| mekânsal blok, blok boyutu | spatial block, block size |
| tampon, tampon uzaklığı, tamponlu | buffer, buffer distance, buffered |
| kullanıcı tanımlı katlar | user-defined folds |
| en yakın komşu uzaklığı eşleme | k-fold nearest neighbor distance matching (kNNDM) |
| çevresel kümeler | environmental clusters |
| zaman sıralı doğrulama, zamansal ayırma | time-ordered validation, temporal split |
| dönem | period |
| olasılıklı alan doğrulaması | design-based map validation (probability sample) |
| alan doğruluğu | map accuracy |
| rastgelelik tohumu, tohum | random seed, seed |
| bağımsız grup | independent group |
| veri sızıntısı | data leakage |
| dışa taşma, dışa taşıma | extrapolation |

## Preprocessing and variable selection

| Turkish | English |
|---|---|
| ön işleme | preprocessing |
| eksik değer tamamlama | imputation |
| ölçekleme: standart, dayanıklı | scaling: standard, robust |
| hedef dönüşümü, geri dönüşüm | target transformation, back-transformation |
| logaritmik dönüşüm | log transformation |
| üstel geri dönüşüm | exponential back-transformation |
| eğitim artıklarından ortalama düzeltmesi | mean correction from training residuals (smearing) |
| haritalanan büyüklük | mapped quantity |
| çoklu doğrusal bağlantı | multicollinearity |
| varyans şişirme faktörü | variance inflation factor (VIF) |
| eleme, iteratif eleme | elimination, iterative elimination |
| değişken seçimi / indirgeme | variable selection / reduction |
| karşılıklı bilgi | mutual information |
| temel bileşenler analizi | principal component analysis (PCA) |
| özyinelemeli eleme | recursive feature elimination |
| ardışık ileri seçim | sequential forward selection |
| değişken önemi, permütasyonla değişken önemi | variable importance, permutation variable importance |
| Shapley katkı açıklamaları | Shapley value explanations |

## Models and search

| Turkish | English |
|---|---|
| eğitim, eğitim verisi | training, training data |
| nihai model | final model |
| eğitim ortalaması, eğitim medyanı | training mean, training median |
| aday, arama uzayı | candidate, search space |
| deneme bütçesi, süre bütçesi | trial budget, time budget |
| ızgara araması, rastgele arama | grid search, random search |
| ağaç yapılı Parzen kestiricisi | Tree-structured Parzen Estimator (TPE) |
| düzenlileştirme | regularization |
| ağaç sayısı, azami derinlik | number of trees, maximum depth |
| yaprakta asgari gözlem | minimum samples per leaf |
| öğrenme oranı | learning rate |
| destek vektör regresyonu | support vector regression |
| rastgele orman | random forest |
| adi kriging | ordinary kriging |
| regresyon kriging | regression kriging |
| yalın model (regression kriging) | base model |
| yarıvaryogram; küresel, üstel, Gauss | semivariogram; spherical, exponential, Gaussian |
| uzaklık sınıfı sayısı | number of lag classes |
| eğitim turu, eğitim yığını | epochs, batch size |
| gizli katman | hidden layer |
| birim kapatma oranı | dropout rate |
| ağırlık cezası | weight decay |
| topluluk üyesi | ensemble member |
| önceden eğitilmiş | pretrained |
| deneysel | experimental |

## Metrics

| Turkish | English |
|---|---|
| ortalama kare hatanın karekökü | root mean squared error (RMSE) |
| ortalama kare hata | mean squared error (MSE) |
| ortalama mutlak hata | mean absolute error (MAE) |
| medyan mutlak hata | median absolute error |
| ortalama hata | mean error (ME) |
| yanlılık | bias |
| belirleme katsayısı | coefficient of determination (R²) |
| Pearson korelasyon katsayısı | Pearson correlation coefficient (r) |
| Lin uyum korelasyon katsayısı | Lin's concordance correlation coefficient (CCC) |
| model etkinlik katsayısı | model efficiency coefficient (MEC) |
| çeyrekler arası aralık | interquartile range (IQR) |
| birinci çeyrek, üçüncü çeyrek | first quartile, third quartile |
| artık | residual |
| hata ölçütü, birincil hata ölçütü | error metric, primary error metric |

## Uncertainty

| Turkish | English |
|---|---|
| belirsizlik | uncertainty |
| tahmin aralığı | prediction interval |
| güven aralığı | confidence interval |
| kapsama oranı | coverage |
| ampirik kapsama oranı | empirical coverage |
| istenen kapsama oranı, nominal kapsama | nominal coverage |
| aralık skoru | interval score |
| alt kantil kaybı, üst kantil kaybı | lower pinball loss, upper pinball loss |
| kesişen kantiller | crossing quantiles |
| kalibrasyon, kalibrasyon oranı | calibration, calibration fraction |
| bölünmüş konformal tahmin | split conformal prediction |
| konformalize kantil regresyonu | conformalized quantile regression (CQR) |
| kantil regresyon ormanı | quantile regression forest |
| kantil kayıplı gradyan artırma | gradient boosting with quantile loss |
| mekânsal ağırlıklı konformal tahmin | spatially weighted conformal prediction |
| grup bootstrap yayılımı | group bootstrap spread |
| kriging varyansı | kriging variance |

## Mapping and applicability

| Turkish | English |
|---|---|
| haritalama | mapping |
| uygulanabilirlik alanı | area of applicability (AOA) |
| benzemezlik indeksi, çevresel benzemezlik | dissimilarity index (DI), environmental dissimilarity |
| alan içinde, alan dışında | inside the area, outside the area |
| referans grid, hizalama | reference grid, alignment |
| yeniden örnekleme, en yakın hücre | resampling, nearest cell |
| işleme penceresi | processing window |
| eğitim aralığı | training range |
| fiziksel alt sınır, fiziksel üst sınır | physical lower bound, physical upper bound |
| maskeleme | masking |
| şekil çözünürlüğü, nokta/inç | figure resolution, dots per inch (dpi) |
| alt grup tanısı | subgroup diagnostics |

## Further recurring terms

| Turkish | English | Note |
|---|---|---|
| yerel (the C# add-in, its contract tests) | native | "Yerel sözleşme testleri" = native contract tests |
| yerel (otherwise) | local | local MCP server, local report |
| bildirim, sonuç bildirimi | manifest, result manifest | |
| Aşama A, B, C, D | Stage A, B, C, D | development stages in the registry |
| kabul raporu | acceptance report | |
| tanı | diagnostic, diagnostics | |
| yalnız tanı (VIF mode) | diagnostic only | |
| başarı ölçütleri | performance metrics | |
| dış değerlendirme | outer evaluation | |
| yeniden kullanılmış test | reused test set | |
| bağımlılık grubu | dependence group | |
| başvuru modeli, karşılaştırıcı | baseline model, comparator | training mean and median |
| özellik (model input) | feature | |
| açıklayıcı değişken | covariate | |
| etkin örnek sayısı | effective sample size | |
| çekirdek, bant genişliği, sınırlı destek | kernel, bandwidth, compact support | |
| iki doğrusal enterpolasyon | bilinear interpolation | |
| sonlu olmayan | nonfinite | spelling used throughout the code |
| asgari, azami | minimum, maximum | |
| grafik, vektörel grafik | figure, vector graphic | |
| dizin, iş dizini | folder, job folder | |
| çalışma ortamı (Python or R environment) | runtime environment | neither run nor study |
| eklenti | add-in | |
| kurucu, kurulum, kaldırma | installer, installation, uninstallation | |
| kilit dosyası | lock file | |
| yorumlu JSON | JSON with comments (JSONC) | |
| gizlendi | hidden | MCP redaction markers |
| iptal edildi | canceled | the status value `cancelled` is an identifier and stays |

## Strings that stay in Turkish

Two kinds of string keep Turkish characters on purpose:

1. Test data that checks non-ASCII handling, such as `Türkçe ölçüm`, `ıİğĞşŞ`, `sps-ç`. These strings test Unicode paths, files and JSON. They are not interface text.
2. Strings in `build/` and `installer/` acceptance scripts that must match log lines or report text recorded during the Turkish 0.5.0 acceptance. These scripts verify archived evidence of that build. Each such string carries a comment that says so.
