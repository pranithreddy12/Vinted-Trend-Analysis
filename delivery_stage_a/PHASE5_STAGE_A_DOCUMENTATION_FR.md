# Phase 5 — Étape A : Identification des produits par IA
### Documentation de livraison (Français)

**Date :** 3 août 2026
**Composant :** Identification des produits par IA visuelle, pour l'outil d'intelligence de marché Vinted
**Statut :** Livré et validé

---

## 1. Ce que fait l'Étape A

L'Étape A ajoute la **reconnaissance d'image par IA** au moteur de suivi des ventes existant.
À partir de la **photo** d'une annonce, elle identifie le produit exact et génère un **titre
complet et précis** — par exemple, à partir d'une photo floue intitulée seulement *« Gourde
Stanley »*, elle renvoie :

> **Stanley Quencher H2.0 FlowState Tumbler — Rose — 40oz (1.18L)**

Cela résout le problème de fond : les titres des annonces sont incohérents, multilingues et
souvent vagues. L'outil peut désormais regrouper et analyser les produits selon **ce qu'ils sont
réellement**, et non selon le texte saisi par le vendeur.

---

## 2. Précision mesurée

L'identification a été testée **uniquement à partir de la photo** — le titre de l'annonce était
**masqué** pour l'IA, qui travaillait donc purement à partir de l'image — puis comparée au vrai
titre servant de référence :

| Mesure | IA visuelle (Étape A) | Ancien prototype local |
|---|---|---|
| Identification correcte de la ligne de produit | **96 %** | ~73 % |
| Couverture (articles identifiables) | **96 %** | inférieure |

L'IA identifie **davantage** d'articles **et** de façon **plus précise** que l'approche
précédente. Vous pouvez reproduire ce chiffre vous-même à tout moment (voir §5).

---

## 3. D'où viennent les données (la précision par conception)

Pour garantir des titres fiables, chaque attribut provient de sa source la plus fiable :

- **Ligne / modèle du produit** (Quencher, Flip Straw, Polo…) → à partir de la **photo** (l'IA).
  C'est ce pour quoi l'IA est réellement fiable.
- **Couleur** → à partir de la **couleur déclarée dans l'annonce** (Vinted rend la couleur
  obligatoire), et *non* d'une supposition sur la photo. Cela a corrigé la faiblesse précédente
  où une gourde corail pouvait être mal étiquetée.
- **Taille (vêtements)** → à partir du **champ taille obligatoire** de l'annonce, ajoutée
  automatiquement (ex. *« Ralph Lauren Polo Shirt — S »*).
- **Contenance (gourdes)** → lue dans le **texte du titre** (une photo ne peut pas montrer des
  litres).

En résumé : l'IA identifie le *produit* ; les *attributs* proviennent des champs obligatoires de
l'annonce. C'est ce qui rend les titres fiables.

---

## 4. Comment l'activer

L'Étape A est **optionnelle** et désactivée par défaut : rien ne change dans vos exécutions
habituelles tant que vous ne l'activez pas.

**Configuration unique** (votre propre clé API Anthropic — la clé reste sur votre machine, elle
n'est jamais stockée dans le code) :

```
pip install -U anthropic
setx ANTHROPIC_API_KEY   "sk-ant-..."           # votre clé
setx ANTHROPIC_BASE_URL  "https://api.anthropic.com"
setx VINTED_VISION_PROVIDER "anthropic"
```

Ouvrez ensuite une **nouvelle** fenêtre de terminal (pour que les variables soient chargées)
avant de lancer l'outil.

**Lancer le suivi avec l'identification activée :**

```
setx VINTED_VISION 1
python track_sales.py "stanley quencher"
```

Chaque exécution identifie les produits non encore vus et enregistre les résultats (voir §6).

---

## 5. Vérifier la précision vous-même

Test de précision à partir de la photo seule (titre masqué), quelques centimes sur votre clé :

```
python eval_vision.py "stanley quencher" --n 25
```

Rapport visuel — une page HTML autonome montrant chaque photo à côté de ce que l'IA a répondu,
avec un badge HIT/MISS (ouvrable dans n'importe quel navigateur, facile à partager) :

```
python eval_vision.py "stanley quencher" --show --n 25 --html rapport_stanley.html
```

---

## 6. Ce qu'elle produit

- **`product_identities_<produit>.csv`** — une ligne par annonce : le titre complet généré, la
  marque, la ligne de produit, la catégorie, la couleur, la taille, et le niveau de confiance de
  l'IA.
- **`variant_report_<produit>.csv`** — reçoit une colonne **`ai_product`** : le nom de produit
  dominant identifié par l'IA pour chaque variante, aux côtés des indicateurs existants
  (ventes / vélocité / concurrence).
- **Rapports photo HTML** (via `--html`) pour une revue visuelle.

---

## 7. Maîtrise des coûts

Le coût dépend du nombre de **produits nouveaux et distincts**, et non du nombre d'annonces :

- **Cache par annonce** — chaque annonce est identifiée **une seule fois, définitivement**. Les
  exécutions suivantes réutilisent le résultat enregistré et ne coûtent rien.
- **Priorité à la demande** — le budget est dépensé d'abord sur les articles les plus demandés
  (selon les favoris).
- **Plafond par exécution** — `VINTED_VISION_MAX_NEW` limite le nombre de nouvelles
  identifications par exécution.
- **Le plafond de dépense de votre compte** (les ~20 $ que vous avez fixés) est la limite
  absolue.

---

## 8. Périmètre et limites (en toute transparence)

- L'IA identifie de façon fiable la **ligne de produit**. Elle ne reconstitue **pas** le nom
  marketing exact de la couleur (elle donne *« Pink »*, et non *« Rose Quartz »*) — la couleur est
  reprise de manière canonique depuis l'annonce, ce qui est toujours correct et idéal pour le
  regroupement.
- **La confiance est un signal fort mais pas une garantie** — un petit nombre d'erreurs
  d'identification « confiantes » peut survenir (mesuré : ~1 sur 25).
- **Les produits génériques sans marque** (ex. des « escaliers pour chien » sans marque) sont
  décrits (catégorie + couleur + taille) mais non rattachés à un produit de référence précis.
  L'identification approfondie des produits génériques avec recherche externe relève de
  l'**Étape B**.
- La **vérification Google automatique** des titres (optionnelle) est développée mais inactive :
  elle nécessite une clé API de recherche pour être activée.

---

## 9. Récapitulatif des prérequis

- Votre propre **clé API Anthropic** avec un plafond de facturation/dépense (vous gardez le
  contrôle du coût).
- Le paquet Python `anthropic` (`pip install -U anthropic`, version 0.69 ou plus récente).
- Aucun Chrome/connexion nécessaire pour l'identification (le catalogue est lu anonymement).
- Le coût récurrent par image reste faible et plafonné ; facturé via votre clé. Il est intégré au
  coût de fonctionnement mensuel.

---

*Préparé pour leslie570 — Livraison de la Phase 5, Étape A.*
