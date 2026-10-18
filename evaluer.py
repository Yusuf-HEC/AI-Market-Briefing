"""Évaluation du briefing de marché, sans modifier briefing.py.

Place ce fichier à côté de briefing.py, puis lance les commandes dans l'ordre :

  python evaluer.py collecter                      # sauvegarde les articles du jour dans evaluation/articles_DATE.json
  python evaluer.py rejouer                        # génère un briefing depuis ces articles + contrôles automatiques
  python evaluer.py rejouer --juge                 # idem + vérification des faits par un second appel au modèle
  python evaluer.py rejouer --model autre/modele   # mêmes articles, autre modèle : comparaison équitable
  python evaluer.py etiqueter                      # crée un CSV d'articles à étiqueter à la main
  python evaluer.py routage                        # précision du routage par mots-clés, depuis le CSV rempli

Les commandes « rejouer » ont besoin de la variable d'environnement OPENROUTER_API_KEY.
Chaque « rejouer » ajoute une ligne à evaluation/resultats.csv : c'est ton tableau de bord.

Limites à connaître :
- Les contrôles de chiffres sont une heuristique : un arrondi (4,2 au lieu de 4,25) sera signalé.
  La liste des chiffres suspects est faite pour être vérifiée à la main.
- Le juge est un modèle : il peut se tromper. Relis une vingtaine de ses verdicts avant de lui faire confiance.
- Le routage est mesuré en précision seulement (parmi les articles rangés dans une rubrique, combien y
  avaient leur place). Le rappel exigerait de garder aussi les articles écartés, ce que collecter() ne fait pas.
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
from datetime import date

from openai import OpenAI

import briefing  # le briefing.py du dépôt, importé tel quel

DOSSIER = 'evaluation'
RESULTATS = os.path.join(DOSSIER, 'resultats.csv')
CODE = re.compile(r'\b[TFCAO]\d+\b')
NOMBRE = re.compile(r'\d+(?:[.,]\d+)*')

SYSTEM_JUGE = '''Tu es un vérificateur factuel. On te donne des articles identifiés par des codes entre crochets, puis une analyse qui s'appuie sur eux.
Découpe l'analyse en affirmations factuelles (chiffres, événements, attributions de propos) et, pour chacune, donne un verdict :
- SOUTENUE : un article fourni dit clairement la même chose.
- NON_SOUTENUE : aucun article fourni ne dit cela, ou un article dit autre chose.
- NON_VERIFIABLE : l'affirmation est trop vague pour être vérifiée.
Ne juge pas les opinions ni les commentaires du modèle, seulement les faits.
Réponds uniquement par un tableau JSON, sans autre texte :
[{"affirmation": "...", "verdict": "SOUTENUE"}]'''


# ---------------------------------------------------------------- utilitaires
def chemin_articles(chemin=None):
    if chemin:
        return chemin
    fichiers = sorted(glob.glob(os.path.join(DOSSIER, 'articles_*.json')))
    if not fichiers:
        sys.exit("Aucun fichier d'articles : lance d'abord « python evaluer.py collecter ».")
    return fichiers[-1]


def charger_articles(chemin):
    with open(chemin, encoding='utf-8') as f:
        return json.load(f)


def creer_client():
    cle = os.environ.get('OPENROUTER_API_KEY')
    if not cle:
        sys.exit("OPENROUTER_API_KEY manquante (variable d'environnement).")
    return OpenAI(api_key=cle, base_url='https://openrouter.ai/api/v1', max_retries=5)


def bloc_articles(liste):
    return '\n\n'.join('[{id}] {source} | {date} | {nature} | {titre}\n{texte}'.format(**a) for a in liste)


def lire_json(texte):
    try:
        return json.loads(texte[texte.index('['):texte.rindex(']') + 1])
    except Exception:
        return None


# ---------------------------------------------------------------- contrôles automatiques
def codes_cites(texte):
    """Tous les codes de sources cités entre crochets, y compris [T1, T2]."""
    trouves = []
    for groupe in re.findall(r'\[([^\]]+)\]', texte):
        trouves += CODE.findall(groupe)
    return trouves


def nombres(texte):
    """Dictionnaire {chiffres seuls: forme d'origine}. On compare sans séparateurs : 4,25 et 4.25 -> 425."""
    texte = re.sub(r'\[[^\]]*\]', ' ', texte)            # retire les codes de sources
    texte = re.sub(r'\d{4}-\d{2}-\d{2}', ' ', texte)     # retire les dates ISO
    texte = re.sub(r'(?m)^\s*\d+[.)]\s+', '', texte)     # retire la numérotation des listes
    res = {}
    for jeton in NOMBRE.findall(texte):
        chiffres = re.sub(r'\D', '', jeton)
        if len(chiffres) < 2 or re.fullmatch(r'20\d\d', chiffres):
            continue
        res.setdefault(chiffres, jeton)
    return res


def controles(texte_briefing, texte_analyses, articles):
    valides = {a['id'] for liste in articles.values() for a in liste}
    cites = codes_cites(texte_briefing + '\n' + texte_analyses)
    invalides = sorted({c for c in cites if c not in valides})

    sources = ' '.join(a['titre'] + ' ' + a['texte'] for liste in articles.values() for a in liste)
    connus = set(nombres(sources))
    produits = nombres(texte_briefing + '\n' + texte_analyses)
    suspects = sorted(produits[c] for c in produits if c not in connus)
    return {'n_codes': len(cites), 'codes_invalides': invalides,
            'n_nombres': len(produits), 'nombres_suspects': suspects}


# ---------------------------------------------------------------- juge (second appel au modèle)
def juger(cl, articles, texte_analyses, modele_juge):
    """Un appel par rubrique : le juge reçoit les articles et l'analyse, et rend un verdict par affirmation."""
    parties = re.split(r'^=== ([A-Z]+) ===\n', texte_analyses, flags=re.M)
    verdicts, erreurs = [], 0
    ancien = briefing.MODEL
    briefing.MODEL = modele_juge
    try:
        for nom, texte in zip(parties[1::2], parties[2::2]):
            rub = nom.lower()
            liste = articles.get(rub) or []
            if not liste or texte.strip().lower().startswith('non couvert'):
                continue
            rep = briefing.appeler(cl, SYSTEM_JUGE, f'ARTICLES\n{bloc_articles(liste)}\n\nANALYSE\n{texte}')
            donnees = lire_json(rep)
            if donnees is None:
                erreurs += 1
                continue
            for v in donnees:
                verdicts.append({'rubrique': rub,
                                 'affirmation': v.get('affirmation', ''),
                                 'verdict': str(v.get('verdict', '')).upper()})
    finally:
        briefing.MODEL = ancien
    return verdicts, erreurs


# ---------------------------------------------------------------- commandes
def cmd_collecter(args):
    os.makedirs(DOSSIER, exist_ok=True)
    articles = briefing.collecter()
    print('=== FLUX ===')
    for nom, statut, info in briefing.RAPPORT:
        print(f'{statut:7} | {nom} | {info}')
    chemin = os.path.join(DOSSIER, f'articles_{date.today()}.json')
    with open(chemin, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=1)
    print('\nArticles par rubrique :', {r: len(l) for r, l in articles.items()})
    print('Enregistré :', chemin)


def cmd_rejouer(args):
    chemin = chemin_articles(args.articles)
    articles = charger_articles(chemin)
    if not any(articles.values()):
        sys.exit("Ce fichier ne contient aucun article.")
    cl = creer_client()
    if args.model:
        briefing.MODEL = args.model
    modele = briefing.MODEL
    briefing.tot['in'] = briefing.tot['out'] = 0

    print(f'Génération avec {modele} à partir de {chemin} ...')
    texte_briefing, texte_analyses = briefing.produire(cl, articles)
    tokens_in, tokens_out = briefing.tot['in'], briefing.tot['out']
    cout = (tokens_in * briefing.PRIX_ENTREE + tokens_out * briefing.PRIX_SORTIE) / 1e6

    os.makedirs(os.path.join(DOSSIER, 'sorties'), exist_ok=True)
    slug = modele.replace('/', '_')
    sortie = os.path.join(DOSSIER, 'sorties', f'briefing_{slug}_{date.today()}.md')
    with open(sortie, 'w', encoding='utf-8') as f:
        f.write(texte_briefing + '\n\n## Analyses par rubrique\n\n' + texte_analyses)

    c = controles(texte_briefing, texte_analyses, articles)
    print(f"\nCodes de sources cités : {c['n_codes']} | codes inconnus : {len(c['codes_invalides'])} {c['codes_invalides']}")
    print(f"Nombres produits : {c['n_nombres']} | absents des sources : {len(c['nombres_suspects'])}")
    if c['nombres_suspects']:
        print('  À vérifier à la main :', ', '.join(c['nombres_suspects']))

    n_aff, taux, modele_juge = '', '', ''
    if args.juge:
        modele_juge = args.modele_juge or modele
        if modele_juge == modele:
            print("\nAttention : le juge est le même modèle que l'auteur. Pour un jugement moins complaisant, "
                  "utilise --modele-juge avec un autre modèle.")
        verdicts, erreurs = juger(cl, articles, texte_analyses, modele_juge)
        n_aff = len(verdicts)
        soutenues = sum(v['verdict'] == 'SOUTENUE' for v in verdicts)
        taux = round(soutenues / n_aff, 3) if n_aff else ''
        fichier_juge = os.path.join(DOSSIER, 'sorties', f'juge_{slug}_{date.today()}.json')
        with open(fichier_juge, 'w', encoding='utf-8') as f:
            json.dump(verdicts, f, ensure_ascii=False, indent=1)
        print(f'\nJuge ({modele_juge}) : {soutenues}/{n_aff} affirmations soutenues'
              f'{f" (taux {taux})" if taux != "" else ""}, {erreurs} rubrique(s) illisible(s).')
        for v in verdicts:
            if v['verdict'] != 'SOUTENUE':
                print(f"  [{v['verdict']}] ({v['rubrique']}) {v['affirmation']}")
        print('Relis une vingtaine de verdicts à la main :', fichier_juge)

    nouveau = not os.path.exists(RESULTATS)
    with open(RESULTATS, 'a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, delimiter=';')
        if nouveau:
            w.writerow(['date', 'modele', 'modele_juge', 'articles', 'codes', 'codes_inconnus', 'nombres',
                        'nombres_suspects', 'affirmations_jugees', 'taux_soutenu', 'tokens_entree',
                        'tokens_sortie', 'cout_estime'])
        w.writerow([date.today(), modele, modele_juge, os.path.basename(chemin), c['n_codes'],
                    len(c['codes_invalides']), c['n_nombres'], len(c['nombres_suspects']), n_aff, taux,
                    tokens_in, tokens_out, f'{cout:.5f}'])
    print(f"\nTokens entrée {tokens_in} | sortie {tokens_out} | coût estimé {cout:.5f}")
    print('Briefing :', sortie, '| tableau de bord :', RESULTATS)


def cmd_etiqueter(args):
    chemin = chemin_articles(args.articles)
    articles = charger_articles(chemin)
    cible = os.path.basename(chemin).replace('articles_', 'etiquettes_').replace('.json', '.csv')
    cible = os.path.join(DOSSIER, cible)
    if os.path.exists(cible):
        sys.exit(f'{cible} existe déjà : je ne l\'écrase pas, pour ne pas perdre tes étiquettes.')
    with open(cible, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(['rubrique', 'id', 'source', 'titre', 'url', 'correct'])
        for rub, liste in articles.items():
            for a in liste:
                w.writerow([rub, a['id'], a['source'], a['titre'], a['url'], ''])
    print(f"Créé : {cible}")
    print("Ouvre-le (Excel, LibreOffice, VS Code) et remplis la colonne « correct » : "
          "1 si l'article a sa place dans la rubrique, 0 sinon. Puis : python evaluer.py routage")


def cmd_routage(args):
    chemin = args.fichier
    if not chemin:
        fichiers = sorted(glob.glob(os.path.join(DOSSIER, 'etiquettes_*.csv')))
        if not fichiers:
            sys.exit("Aucun fichier d'étiquettes : lance d'abord « python evaluer.py etiqueter ».")
        chemin = fichiers[-1]
    oui, non = {'1', 'oui', 'o', 'yes', 'y', 'x'}, {'0', 'non', 'n', 'no'}
    stats = {}
    with open(chemin, newline='', encoding='utf-8-sig') as f:
        for ligne in csv.DictReader(f, delimiter=';'):
            val = (ligne.get('correct') or '').strip().lower()
            if val not in oui | non:
                continue
            s = stats.setdefault(ligne['rubrique'], [0, 0])
            s[0] += 1
            s[1] += val in oui
    if not stats:
        sys.exit("Aucune étiquette remplie dans la colonne « correct ».")
    print(f'Précision du routage par mots-clés ({chemin}) :')
    total = bon = 0
    for rub, (n, b) in stats.items():
        print(f'  {rub:12} {b}/{n} = {b / n:.0%}')
        total += n
        bon += b
    print(f'  {"ensemble":12} {bon}/{total} = {bon / total:.0%}')
    print("Précision seulement : le rappel demanderait d'étiqueter aussi des articles écartés par le routage.")


def main(argv=None):
    p = argparse.ArgumentParser(description="Évaluation du briefing de marché.")
    sp = p.add_subparsers(dest='commande', required=True)

    sp.add_parser('collecter', help="sauvegarde les articles du jour").set_defaults(fn=cmd_collecter)

    r = sp.add_parser('rejouer', help="regénère un briefing depuis des articles sauvegardés et le contrôle")
    r.add_argument('--articles', help="fichier d'articles (par défaut : le plus récent)")
    r.add_argument('--model', help="modèle à utiliser à la place de celui de briefing.py")
    r.add_argument('--juge', action='store_true', help="ajoute la vérification des faits par un modèle")
    r.add_argument('--modele-juge', help="modèle du juge (par défaut : le même que l'auteur)")
    r.set_defaults(fn=cmd_rejouer)

    e = sp.add_parser('etiqueter', help="crée le CSV à remplir à la main")
    e.add_argument('--articles', help="fichier d'articles (par défaut : le plus récent)")
    e.set_defaults(fn=cmd_etiqueter)

    g = sp.add_parser('routage', help="calcule la précision du routage depuis le CSV rempli")
    g.add_argument('--fichier', help="CSV d'étiquettes (par défaut : le plus récent)")
    g.set_defaults(fn=cmd_routage)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == '__main__':
    main()
