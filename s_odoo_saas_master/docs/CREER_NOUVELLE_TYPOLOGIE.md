# 🏗️ Créer une nouvelle typologie Docker (s_odoo_saas_master)

> Guide opérationnel — Phase 3. Valable pour les deux serveurs (SSD Nodes prod + Contabo DR).

## 1. C'est quoi une « typologie » ?

Une typologie = un **tag business** (`saas.instance.tag`) relié à une **image Docker**
(`saas.docker.image`) qui embarque les dépendances nécessaires à des custom-addons
(ex. : BTP → qifparse/reportlab, OCR → tesseract/opencv, BTP-OCR → les deux).

```
Tag (BTP, OCR, BTP-OCR, Standard...)
   └── saas.docker.image (odoo:19.0-cmy-btp, ...)
          └── instances Odoo (champ docker_image_id)
```

> **Convention de nommage** : `odoo:<version>-<édition>-<typologie>`
> - `<édition>` = `cmy` (Community) ou `ent` (Enterprise)
> - Ex. : `odoo:19.0-cmy-btp` (community), `odoo:19.0-ent-btp` (enterprise)

## 2. Étapes

### 2.1 Créer (ou réutiliser) le tag business
Menu **Configuration → Odoo → Tags** (ou équivalent `saas.instance.tag`).
- Ex. : `BTP`, `OCR`, `BTP-OCR`, `Standard`.

### 2.2 Créer l'image Docker
Menu **Configuration → Odoo → Docker Images** → Nouveau :

| Champ | Exemple | Notes |
|---|---|---|
| Name | BTP OCR 19 | Nom interne |
| Odoo Version | 19.0 | Version cible |
| Business Typology | BTP-OCR | Le tag créé en 2.1 |
| Edition | `community` | Community ou Enterprise — doit apparaître dans le nom |
| Docker Image Name | `odoo:19.0-cmy-btp-ocr` | **Unique** (contrainte SQL) — convention `odoo:<version>-<édition>-<typologie>` (`cmy`=community, `ent`=enterprise) |
| Base Image | `odoo:19.0` | Image officielle de base |
| System Packages | `python3-dev`, `build-essential`, `libcairo2`, … | Paquets apt (1 par ligne) |
| Python Requirements | `qifparse`, `reportlab`, … | Dépendances pip manuelles (1 par ligne) |
| Ignored Requirements Files | `*ks_dashboard_ninja*` | (optionnel) motifs glob des `requirements*.txt` à ignorer |
| Build Servers | SSD Nodes (+ Contabo en DR) | Serveurs où l'image doit exister |

> 💡 Le **Dockerfile** est généré automatiquement (onglet « Dockerfile (auto) »).

### 2.3 (Optionnel) Aperçu des requirements auto-détectés
Bouton **« Détecter requirements »** : scanne les `requirements*.txt` des custom-addons
sur les serveurs sélectionnés et remplit l'onglet « Auto Requirements (détectés) »
**sans builder**.

### 2.4 Builder l'image
Bouton **« Build Image »** :
1. Connexion SSH à chaque serveur (`saas.pserver`)
2. Écriture du Dockerfile dans `/opt/odoo18/custom-images/<slug>/`
3. **Auto-détection** des `requirements*.txt` :
   - chemins des `saas.odoo.server.extra.addon` du serveur (ex. `/odoo/enterprise19`)
   - découverte des `/home/*/custom-addons` des instances
   - fichiers filtrés : chemins locaux (`./`, `../`, `/`), `-r`/`-c`/`--requirement`/`--constraint`, commentaires
   - fichiers ignorés via `ignored_requirements_files` (glob `fnmatch`)
   - fusion avec les `python_requirements` manuels (dédupliquée)
4. Si des requirements auto sont trouvés : écriture d'un `requirements.txt` +
   Dockerfile avec `COPY` + `pip install --break-system-packages -r` (+ `setuptools wheel`)
5. `docker build -t <image_name> .` → log capturé dans « Build Log », statut `built`/`failed`

### 2.5 Rattacher l'image aux instances
Sur l'instance (ou le template) : champ **Docker Image** (`docker_image_id`),
filtré par version Odoo. Le docker-compose généré utilisera alors
`image: odoo:19.0-cmy-btp-ocr` au lieu de `odoo:19.0`.

## 3. Règles & pièges connus

| Sujet | Détail |
|---|---|
| **PEP 668** | Images Debian récentes : `pip install` système bloqué → le générateur utilise `--break-system-packages` |
| **Pins incompatibles** | Un `requirements*.txt` peut épingler une version sans wheel pour le Python de l'image (ex. `pandas==2.0.3` sur Python 3.12 → échec build source, `pkg_resources` manquant dans l'isolation pip). → Utiliser `ignored_requirements_files` OU mettre à jour le pin dans l'addon |
| **Chemins locaux** | Les lignes type `./addons/.../fichier.whl` sont **ignorées** (non portables dans le contexte de build) |
| **Fichiers sans retour à la ligne** | Gérés : chaque fichier est parsé individuellement |
| **Édition enterprise (`ent`)** | Les instances enterprise actuelles montent `/odoo/enterprise19:/mnt/standard-extra-addons` sur une image **community**. Une vraie image `odoo:19.0-ent-*` embarquerait les addons enterprise dans le build (FROM image enterprise ou COPY des addons) |
| **Build sur Contabo (DR)** | Règle en vigueur : **ne pas toucher Contabo** tant que la validation myodoo.nc n'est pas terminée. Pour la bascule DR : ré-uploader la clé SSH « DockerPortal » dans l'attachment Odoo, puis builder les images sur Contabo |
| **`docker build` long** | Les images typologie pèsent 2,7 à 3,1 Go ; prévoir plusieurs minutes |

## 4. Vérifications post-build

```bash
# L'image existe sur le serveur
docker images | grep <image_name>

# Le compose d'une instance utilise la bonne image
grep image /home/<instance>/docker-compose.yml

# Les containers tournent avec la bonne image
docker ps --format '{{.Names}} {{.Image}}' | grep <instance>
```
