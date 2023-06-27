# Installation de la plateforme DSO

## Sommaire
- [Installation de la plateforme DSO](#installation-de-la-plateforme-dso)
  - [Sommaire](#sommaire)
  - [Introduction](#introduction)
  - [Prérequis](#prérequis)
  - [Configuration](#configuration)
  - [Installation](#installation)
    - [Lancement](#lancement)
    - [Déploiement de plusieurs forges DSO dans un même cluster](#déploiement-de-plusieurs-forges-dso-dans-un-même-cluster)
  - [Récupération des secrets](#récupération-des-secrets)
  - [Debug](#debug)
    - [Réinstallation](#réinstallation)
    - [Keycloak](#keycloak)
  - [Désinstallation](#désinstallation)
  - [Gel des versions](#gel-des-versions)
    - [Argo CD](#argo-cd)
      - [Gel de l'image](#gel-de-limage)

## Introduction

L'installation de la forge DSO (DevSecOps) se fait **via Ansible**. Chaque élément sera donc installé de manière automatisée. Certains peuvent prendre un peu de temps, par exemple Keycloak ou GitLab.

## Prérequis

Cette installation s'effectue dans un cluster OpenShift opérationnel et correctement démarré.

Elle nécessite l'utilisation de [ce dépôt](https://github.com/cloud-pi-native/socle) qui devra donc être cloné sur votre environnement de déploiement ([Ansible control node](https://docs.ansible.com/ansible/latest/network/getting_started/basic_concepts.html#control-node)).

Elle nécessitera d'avoir installé au préalable les éléments suivants **sur l'environnement de déploiement** :

- Modules python :
  - pyyaml
  - kubernetes
  - python-gitlab

Exemple d'installation du module python-gitlab pour l'utilisateur courant :

```bash
python3 -m pip install --user python-gitlab
```

- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/intro_installation.html) (pour disposer de la commande ansible-playbook).

  - Ainsi que les collections suivantes :
    - [kubernetes.core](https://github.com/ansible-collections/kubernetes.core)
    - [community.general](https://github.com/ansible-collections/community.general)

- La commande [kubectl](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/).

- Un accès administrateur au cluster.

- Le fichier de configuration ```~/.kube/config``` paramétré avec les accès administrateur, pour l'appel à l'API du cluster (section users).

- La commande [helm](https://helm.sh/docs/intro/install/).

- La commande [yq](https://github.com/mikefarah/yq/#install) (facultative, utile pour debug).

- L'outil d'encryption [age](https://github.com/FiloSottile/age#installation), qui fournit les commandes `age` et `age-keygen` nécessaires pour l'installation de SOPS.

## Configuration

Une fois le dépôt socle cloné, lancez une première fois la commande suivante depuis votre environnement de déploiement :

```bash
ansible-playbook install.yaml
```

Elle vous signalera que vous n'avez encore jamais installé le socle sur votre cluster, puis vous invitera à modifier la resource de scope cluster et de type **dsc** nommée **conf-dso** via la commande suivante :

```bash
kubectl edit dsc conf-dso
```

Lancer la commande ci-dessus pour éditer la ressource indiquée.

Alternativement, et comme précisé, vous pourrez aussi déclarer la ressource dsc `conf-dso` dans un fichier YAML, nommé par exemple « ma-conf-dso.yaml », puis la créer via la commande suivante :

```bash
kubectl apply -f ma-conf-dso.yaml
```

Voici un **exemple** de fichier de configuration valide, à adapter à partir de la section **spec**, notamment au niveau des mots de passe, des numéros de versions et des namespaces :

```yaml
---
kind: DsoSocleConfig
apiVersion: cloud-pi-native.fr/v1alpha
metadata:
  name: conf-dso
spec:
  additionalsCA:
    - kind: ConfigMap
      name: kube-root-ca.crt
  exposedCA:
    type: configmap
    configmap:
      namespace: default
      name: ca-cert
      key: ingress.crt
  argocd:
    admin:
      enabled: true
      password: WeAreThePasswords
    namespace: mynamespace-argocd
    subDomain: argocd
    chartVersion: "4.7.13"
    values:
      image:
        registry: docker.io
        repository: bitnami/argo-cd
        tag: 2.7.6-debian-11-r2
        imagePullPolicy: IfNotPresent
  certmanager:
    version: v1.11.0
  console:
    dbPassword: AnotherPassBitesTheDust
    namespace: mynamespace-console
    release: 4.1.0
    subDomain: console
  gitlab:
    insecureCI: true
    namespace: mynamespace-gitlab
    subDomain: gitlab
    version: "6.11.5"
  global:
    projectsRootDir:
      - my-root-dir
      - projects-sub-dir
    rootDomain: .example.com
  harbor:
    adminPassword: WhoWantsToPassForever
    namespace: mynamespace-harbor
    subDomain: harbor
  ingress:
    tls:
      type: tlsSecret
      tlsSecret:
        name: ingress-tls
        method: in-namespace
  keycloak:
    namespace: mynamespace-keycloak
    subDomain: keycloak
  nexus:
    namespace: mynamespace-nexus
    subDomain: nexus
    storageSize: 100Gi
  proxy:
    enabled: true
    host: "192.168.xx.xx"
    http_proxy: http://192.168.xx.xx:3128/
    https_proxy: http://192.168.xx.xx:3128/
    no_proxy: .cluster.local,.svc,10.0.0.0/8,127.0.0.1,192.168.0.0/16,api-int.example.com,canary-openshift-ingress-canary.apps.example.com,console-openshift-console.apps.example.com,localhost,oauth-openshift.apps.example.com,svc.cluster.local,localdomain
    port: "3128"
  sonarqube:
    namespace: mynamespace-sonarqube
    subDomain: sonarqube
  sops:
    namespace: mynamespace-sops
  vault:
    namespace: mynamespace-vault
    subDomain: vault
```
## Installation

### Lancement
Dès que votre [configuration](#configuration) est prête, c'est à dire que la resource dsc par défaut  `conf-dso` a bien été mise à jour, relancez la commande suivante :

```bash
ansible-playbook install.yaml
```

Patientez …

Pendant l'installation, et si vous avez correctement nommé vos namespaces en utilisant un même suffixe ou préfixe, vous pourrez surveiller l'arrivée de ces namespaces dans le cluster comme ceci (exemple avec des namespaces préfixés « mynamespace- »):

```bash
watch "kubectl get ns | grep 'mynamespace-'"
```

### Déploiement de plusieurs forges DSO dans un même cluster

Suite à une première installation réussie et selon vos besoins, il est possible d'installer dans un même cluster une ou plusieurs autres forges DSO, en parallèle de celle installée par défaut.

Pour cela, il vous suffit de déclarer une **nouvelle ressource de type dsc dans le cluster**, en la nommant différemment de la resource dsc par défaut qui, pour rappel, se nomme `conf-dso`, et en y modifiant les noms des namespaces.

Comme vu plus haut dans la section [Configuration](#configuration), déclarez votre ressource dsc personnalisée **dans un fichier YAML**.

Il s'agira simplement de **modifier le nom de la ressource dsc** (section `metadata`, champ `name`) puis **adapter le nom de tous les namespaces de vos outils** (en les préfixant ou suffixant différemment de ceux déclarés dans la dsc `conf-dso`).

Adaptez également si nécessaire les autres éléments de votre nouvelle configuration (mots de passe, ingress, CA, domaines, proxy …).

Lorsque celle-ci est prête, et déclarée par exemple dans le fichier « ma-conf-perso.yaml », créez-là dans le cluster comme ceci :

```bash
kubectl apply -f ma-conf-perso.yaml
```

Vous pourrer ensuite la retrouver via la commande :

```bash
kubectl get dsc
```

Puis éventuellement l'afficher (exemple avec une dsc nommée `ma-dsc`) :

```bash
kubectl get dsc ma-dsc -o yaml
```

Dès lors, il vous sera possible de déployer une nouvelle chaîne DSO  dans ce cluster, en plus de celle existante. Pour cela, vous utiliserez l'extra variable prévue à cet effet, nommée `dsc_cr` (pour DSO Socle Config Custom Resource).

Par exemple, si votre nouvelle resource dsc se nomme `ma-dsc`, alors vous lancerez l'installation correspondante comme ceci : 

```bash
ansible-playbook install.yaml -e dsc_cr=ma-dsc
```

## Récupération des secrets
Au moment de leur initialisation, certains outils stockent des secrets qui ne sont en principe plus disponibles ultérieurement.

**Attention !** Pour garantir l'[idempotence](https://fr.wikipedia.org/wiki/Idempotence), ces secrets sont stockés dans plusieurs ressources du cluster. Supprimer ces ressources **indique à ansible qu'il doit réinitialiser les composants**.

Afin de faciliter la récupération des secrets, un playbook d'administration nommé « get-credentials.yaml » est mis à disposition dans le répertoire « admin-tools ».

Pour le lancer :

```bash
ansible-playbook admin-tools/get-credentials.yaml
```

Ce playbook permet également de cibler un outil en particulier, grâce à l'utilisation de tags qui sont listés au début de l'exécution, exemple avec keycloak :

```bash
ansible-playbook admin-tools/get-credentials.yaml -t keycloak
```

Enfin, dans le cas où plusieurs chaînes DSO sont déployées dans le même cluster, il permet de cibler la chaîne DSO voulue via l'utilisation de l'extra variable `dsc_cr`, exemple avec la chaîne utilisant la dsc `ma-conf` :

```bash
ansible-playbook admin-tools/get-credentials.yaml -e dsc_cr=ma-conf
```

Et bien sûr cibler un ou plusieurs outils en même temps, via les tags, exemple :

```bash
ansible-playbook admin-tools/get-credentials.yaml -e dsc_cr=ma-conf -t keycloak,argocd
```

**Remarque importante** : Il est **vivement encouragé** de conserver les valeurs qui vous sont fournies par le playbook « get-credentials.yaml ». Par exemple dans un fichier de base de données chiffré de type KeePass ou Bitwarden. Il est toutefois important de **ne pas les modifier ou les supprimer** sous peine de voir certains composants, par exemple Vault, être réinitialisés.

## Debug
### Réinstallation
Si vous rencontrez des problèmes lors de l'éxécution du playbook, vous voudrez certainement relancer l'installation d'un ou plusieurs composants plutôt que d'avoir à tout réinstaller.

Pour cela, vous pouvez utiliser les tags associés aux rôles dans le fichier « install.yaml ».

Voici par exemple comment réinstaller uniquement les composants keycloak et console, dans la chaîne DSO paramétrée avec la dsc par défaut (`conf-dso`), via les tags correspondants :

```bash
ansible-playbook install.yaml -t keycloak,console
```

Si vous voulez en faire autant sur une autre chaîne DSO, paramétrée avec votre propre dsc (nommée par exemple `ma-dsc`), alors vous utiliserez l'extra variable `dsc_cr` comme ceci :

```bash
ansible-playbook install.yaml -e dsc_cr=ma-dsc -t keycloak,console
```

### Keycloak
L'opérateur Keycloak peut être assez capricieux. Son état souhaité est `status.phase == 'reconciling'`.

En cas d'échec lors de l'installation, vous vérifierez ce qu'il en est avec la commande :

```bash
kubectl get keycloak dso-keycloak -n mynamespace-keycloak -o yaml
```

Ou bien si vous avez installé la commande `yq` :

```bash
kubectl get keycloak dso-keycloak -n mynamespace-keycloak -o yaml | yq '.status.phase'
```

Il se peut que Keycloak reste bloqué en status "initializing" mais que tout soit provisionné. Dans ce cas, relancez plutôt le playbook avec l'extra variable `KEYCLOAK_NO_CHECK` comme ceci :

```bash
ansible-playbook install.yaml -e KEYCLOAK_NO_CHECK=
```
## Désinstallation

Un playbook de désinstallation nommé « uninstall.yaml » est disponible.

Il permet de désinstaller toute la chaîne DSO en une seule fois.

Pour le lancer, en vue de désinstaller la chaîne DSO utilisant le dsc par défaut `conf-dso` :

```bash
ansible-playbook uninstall.yaml
```

**Attention !** Si vous souhaitez plutôt désinstaller une autre chaîne, déployée en utilisant votre propre ressource dsc, alors vous devrez utiliser l'extra variable `dsc_cr`, comme ceci (exemple avec une dsc nommée `ma-dsc`) :

```bash
ansible-playbook uninstall.yaml -e dsc_cr=ma-dsc
```

Selon les performances ou la charge de votre cluster, la désinstallation de certains composants (par exemple Harbor) pourra prendre un peu de temps.

Pour surveiller l'état d'une désinstallation en cours il sera possible, si vous avez correctement préfixé ou suffixé vos namespaces dans votre configuration, de vous appuyer sur la commande suivante (exemple avec le préfixe « mynamespace- ») :

```bash
watch "kubectl get ns | grep 'mynamespace-'"
```

Le playbook de désinstallation peut aussi être utilisé pour supprimer un ou plusieurs outils de manière ciblée, via les tags associés.

L'idée est de faciliter leur réinstallation complète, en utilisant ensuite le playbook d'installation (voir la sous-section [Réinstallation](#réinstallation) de la section Debug).

Par exemple, pour désinstaller uniquement les outils Keycloak et ArgoCD configurés avec le dsc par défaut (`conf-dso`), la commande sera la suivante :

````
ansible-playbook uninstall.yaml -t keycloak,argocd
````

Pour faire la même chose sur les mêmes outils, mais s'appuyant sur une autre configuration (via une dsc nommée `ma-dsc`), vous rajouterez là encore l'extra variable `dsc_cr`. Exemple :

````
ansible-playbook uninstall.yaml -t keycloak,argocd -e dsc_cr=ma-dsc
````
**Remarque importante** : Par défaut, le playbook de désinstallation ne supprimera pas la ressource **kubed**, déployée dans le namespace `openshift-infra`. Ceci parce qu'elle pourrait éventuellement être utilisée par une autre instance de la chaîne DSO. Si vous voulez absolument la désinstaller malgré tout, vous pourrez le faire via l'utilisation du tag correspondant (`-t kubed` ou bien `-t confSyncer`).

## Gel des versions

Selon le type d'infrastructure dans laquelle vous déployez, et **en particulier dans un environnement de production**, vous voudrez certainement pouvoir geler (freeze) les versions d'outils utilisées.

Ceci est géré par divers paramètres que vous pourrez spécifier dans la dsc de configuration par défaut (`conf-dso`) ou votre propre dsc.

Les sections suivantes détaillent comment procéder, outil par outil.

### Argo CD

Tel qu'il est conçu, le rôle argocd déploie par défaut la dernière version du [chart helm Bitnami Argo CD](https://docs.bitnami.com/kubernetes/infrastructure/argo-cd) disponible dans le cache des dépôts helm de l'utilisateur.

Ceci est lié au fait que le paramètre de configuration `chartVersion` d'Argo CD, présent dans la dsc par défaut `conf-dso`, est laissé vide (`chartVersion: ""`).

Pour connaître la dernière version du chart helm et de l'application actuellement disponibles dans votre cache local, utilisez la commande suivante : 

```bash
helm search repo argo-cd
```

Exemple de sortie avec un cache de dépôts qui n'est pas à jour :

```
NAME            CHART VERSION   APP VERSION     DESCRIPTION                                       
bitnami/argo-cd 4.7.9          2.7.4           Argo CD is a continuous delivery tool for Kuber...
```

Pour mettre à jour votre cache de dépôts helm, et obtenir ainsi la dernière version du chart et de l'application :

```bash
helm repo update
```

Relancer alors la commande de recherche :

```bash
helm search repo argo-cd
```

Si votre cache n'était pas déjà à jour, la sortie doit maintenant vous indiquer des versions plus récentes.

Pour connaître la liste des versions de charts helm d'Argo CD que vous pouvez maintenant installer, utilisez la commande suivante : 

```bash
  helm search repo -l argo-cd
```

Si vous souhaitez fixer la version du chart helm, et donc celle d'Argo CD, il vous suffira de relever le **numéro de version du chart** désiré, puis l'indiquer dans votre resource `dsc` de configuration.

Par exemple, si vous utilisez la `dsc` par défaut nommée `conf-dso`, vous pourrez éditer le fichier YAML que vous aviez utilisé pour la paramétrer lors de l'installation, puis adapter la section suivante :

```yaml
  argocd:
    admin:
      enabled: true
      password: WeAreThePasswords
    namespace: mynamespace-argocd
    subDomain: argocd
    chartVersion: ""
```

Pour y spécifier la version souhaitée, exemple :

```yaml
  argocd:
    admin:
      enabled: true
      password: WeAreThePasswords
    namespace: mynamespace-argocd
    subDomain: argocd
    chartVersion: "4.7.13"
```

Il vous suffit alors de mettre à jour votre configuration, exemple :

```bash
kubectl apply -f ma-conf-dso.yaml
```

Puis de relancer l'installation d'Argo CD, laquelle mettra à jour la version du chart et l'image associée, sans coupure de service :

```bash
ansible-playbook install.yaml -t argocd
```
#### Gel de l'image

En complément de l'usage du paramètre `chartVersion`, il est également possible de fixer la version d'image d'Argo CD de façon plus fine, en utilisant un tag dit "[immutable](https://docs.bitnami.com/kubernetes/infrastructure/argo-cd/configuration/understand-rolling-immutable-tags)" (**recommandé en production**). 

Les différents tags utilisables pour l'image d'Argo CD sont disponibles ici : https://hub.docker.com/r/bitnami/argo-cd/tags

Les tags dits "immutables" sont ceux qui possèdent un suffixe de type rXX, lequel correspond au numéro de révision. Ils pointent toujours vers la même image. Par exemple le tag "2.7.6-debian-11-r2" est un tag immutable.

Pour spécifier un tel tag, il nous suffira d'éditer la resource `dsc` de configuration (par défaut ce sera la dsc `conf-dso`) et de surcharger les "values" correspondantes du chart helm, en ajoutant celles dont nous avons besoin. Exemple :

```yaml
  argocd:
    admin:
      enabled: true
      password: WeAreThePasswords
    namespace: mynamespace-argocd
    subDomain: argocd
    chartVersion: "4.7.13"
    values:
      image:
        registry: docker.io
        repository: bitnami/argo-cd
        tag: 2.7.6-debian-11-r2
        imagePullPolicy: IfNotPresent
```

Puis relancer l'installation avec le tag `argocd` pour procéder au remplacement par l'image spécifiée, sans coupure de service :

```bash
ansible-playbook install.yaml -t argocd
```

Pour mémoire, les values utilisables sont disponibles ici : https://github.com/bitnami/charts/blob/main/bitnami/argo-cd/values.yaml

Les releases d'Argo CD et leurs changelogs se trouvent ici : https://github.com/argoproj/argo-cd/releases
