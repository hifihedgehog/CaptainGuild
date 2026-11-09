"""Every word the statistics cards and replies show, in English (US), English (UK), French, Spanish (Spain), Dutch and German, and the
way each language writes numbers and dates.

A person's language is the one they picked with /stats language, or else the one their Discord app uses. A typed command carries no app
language, so it reads the server's. Anything else falls back to English (US).

Each string is a row of six, in the order of LANGUAGES. None in the English (UK) column means the same as English (US): the UK column
differs where the two write a date, a time or a number differently, and the English text avoids words the two spell differently.
"""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

LANGUAGES = ("en-US", "en-GB", "fr", "es-ES", "nl", "de")
NAMES = {"en-US": "English (US)", "en-GB": "English (UK)", "fr": "Français", "es-ES": "Español (España)", "nl": "Nederlands", "de": "Deutsch"}
DEFAULT = "en-US"
_INDEX = {code: index for index, code in enumerate(LANGUAGES)}
# Discord's own locale codes, where they differ from ours
_ALIASES = {"en": "en-US", "es": "es-ES", "es-419": "es-ES", "fr-fr": "fr", "nl-nl": "nl", "de-de": "de"}


def pick(*choices: Any) -> str:
    """The first of these that names one of the six languages: a stored choice, a Discord locale, a server's locale."""
    for choice in choices:
        if choice is None:
            continue
        code = str(getattr(choice, "value", choice)).strip()
        if code in _INDEX:
            return code
        lowered = code.lower()
        for known in LANGUAGES:
            if known.lower() == lowered:
                return known
        if lowered in _ALIASES:
            return _ALIASES[lowered]
    return DEFAULT


TEXT: dict[str, tuple[str, str | None, str, str, str, str]] = {
    # ---- cards ----
    "messages": ("Messages", None, "Messages", "Mensajes", "Berichten", "Nachrichten"),
    "voice": ("Voice", None, "Vocal", "Voz", "Spraak", "Sprachchat"),
    "contributors": ("Contributors", None, "Participants", "Participantes", "Deelnemers", "Aktive"),
    "top_members": ("Top members", None, "Membres les plus actifs", "Miembros más activos", "Actiefste leden", "Aktivste Mitglieder"),
    "top_channels": ("Top channels", None, "Salons les plus actifs", "Canales más activos", "Actiefste kanalen", "Aktivste Kanäle"),
    "top_message_members": ("Top members by messages", None, "Membres par messages", "Miembros por mensajes", "Leden op berichten", "Mitglieder nach Nachrichten"),
    "top_voice_members": ("Top members by voice", None, "Membres par vocal", "Miembros por voz", "Leden op spraak", "Mitglieder nach Sprachzeit"),
    "top_message_channels": ("Top channels by messages", None, "Salons par messages", "Canales por mensajes", "Kanalen op berichten", "Kanäle nach Nachrichten"),
    "top_voice_channels": ("Top channels by voice", None, "Salons par vocal", "Canales por voz", "Kanalen op spraak", "Kanäle nach Sprachzeit"),
    "top_emoji": ("Top emoji", None, "Émojis les plus utilisés", "Emojis más usados", "Meestgebruikte emoji", "Häufigste Emojis"),
    "top_activities": ("Top activities", None, "Activités principales", "Actividades principales", "Populairste activiteiten", "Häufigste Aktivitäten"),
    "top_invites": ("Top invites", None, "Meilleures invitations", "Mejores invitaciones", "Beste uitnodigingen", "Beste Einladungen"),
    "top_inviters": ("Top inviters", None, "Meilleurs parrains", "Quién más invita", "Beste uitnodigers", "Aktivste Einladende"),
    "ranks": ("Server ranks", None, "Classement", "Posición", "Positie", "Rang"),
    "level": ("Level", None, "Niveau", "Nivel", "Niveau", "Level"),
    "level_rank": ("Level rank", None, "Rang de niveau", "Puesto por nivel", "Niveaupositie", "Level-Rang"),
    "xp": ("XP", None, "XP", "XP", "XP", "XP"),
    "most_used_app": ("Most used app", None, "Appli la plus utilisée", "App más usada", "Meestgebruikte app", "Meistgenutzte App"),
    "most_used_emoji": ("Most used emoji", None, "Émoji le plus utilisé", "Emoji más usado", "Meestgebruikte emoji", "Meistgenutztes Emoji"),
    "top_text_channel": ("Top text channel", None, "Salon textuel principal", "Canal de texto principal", "Belangrijkste tekstkanaal", "Aktivster Textkanal"),
    "top_voice_channel": ("Top voice channel", None, "Salon vocal principal", "Canal de voz principal", "Belangrijkste spraakkanaal", "Aktivster Sprachkanal"),
    "created": ("Created", None, "Créé le", "Creado el", "Gemaakt op", "Erstellt"),
    "joined": ("Joined", None, "Arrivé le", "Se unió el", "Lid sinds", "Beigetreten"),
    "account_created": ("Account created", None, "Compte créé le", "Cuenta creada el", "Account gemaakt op", "Konto erstellt"),
    "tracked_since": ("Statistics since", None, "Statistiques depuis", "Estadísticas desde", "Statistieken sinds", "Statistik seit"),
    "server_overview": ("Server overview", None, "Vue d'ensemble du serveur", "Resumen del servidor", "Serveroverzicht", "Serverübersicht"),
    "member_overview": ("Member statistics", None, "Statistiques du membre", "Estadísticas del miembro", "Ledenstatistieken", "Mitgliederstatistik"),
    "channel_overview": ("Channel statistics", None, "Statistiques du salon", "Estadísticas del canal", "Kanaalstatistieken", "Kanalstatistik"),
    "category_overview": ("Category statistics", None, "Statistiques de la catégorie", "Estadísticas de la categoría", "Categoriestatistieken", "Kategoriestatistik"),
    "message_overview": ("Message statistics", None, "Statistiques des messages", "Estadísticas de mensajes", "Berichtstatistieken", "Nachrichtenstatistik"),
    "voice_overview": ("Voice statistics", None, "Statistiques vocales", "Estadísticas de voz", "Spraakstatistieken", "Sprachstatistik"),
    "activity_overview": ("Activities", None, "Activités", "Actividades", "Activiteiten", "Aktivitäten"),
    "emoji_overview": ("Emoji", None, "Émojis", "Emojis", "Emoji", "Emojis"),
    "invite_overview": ("Invites", None, "Invitations", "Invitaciones", "Uitnodigingen", "Einladungen"),
    "top_overview": ("Top statistics", None, "Classements", "Clasificaciones", "Ranglijsten", "Bestenlisten"),
    "members_chart": ("Members", None, "Membres", "Miembros", "Leden", "Mitglieder"),
    "joins_and_leaves": ("Joins and leaves", None, "Arrivées et départs", "Entradas y salidas", "Toetredingen en vertrekken", "Beitritte und Austritte"),
    "joins": ("Joins", None, "Arrivées", "Entradas", "Toetredingen", "Beitritte"),
    "leaves": ("Leaves", None, "Départs", "Salidas", "Vertrekken", "Austritte"),
    "statuses": ("Statuses", None, "Statuts", "Estados", "Statussen", "Status"),
    "online": ("Online", None, "En ligne", "En línea", "Online", "Online"),
    "idle": ("Idle", None, "Inactif", "Ausente", "Afwezig", "Abwesend"),
    "dnd": ("Do not disturb", None, "Ne pas déranger", "No molestar", "Niet storen", "Bitte nicht stören"),
    "not_offline": ("Not offline", None, "Pas hors ligne", "No desconectados", "Niet offline", "Nicht offline"),
    "status_estimate": ("Members not offline, as Discord counts them. A breakdown by status needs the Presence intent.", None,
                        "Membres pas hors ligne, selon Discord. Le détail par statut demande l'intent Presence.",
                        "Miembros no desconectados, según Discord. El desglose por estado requiere el intent Presence.",
                        "Leden die niet offline zijn, volgens Discord. Een uitsplitsing per status vraagt de Presence-intent.",
                        "Mitglieder, die nicht offline sind, laut Discord. Eine Aufteilung nach Status braucht den Presence-Intent."),
    "activities_chart": ("Top activities over time", None, "Activités principales dans le temps", "Actividades principales en el tiempo",
                         "Populairste activiteiten door de tijd", "Häufigste Aktivitäten im Zeitverlauf"),
    "emoji_chart": ("Top emoji over time", None, "Émojis principaux dans le temps", "Emojis principales en el tiempo", "Populairste emoji door de tijd",
                    "Häufigste Emojis im Zeitverlauf"),
    "invites_chart": ("Joins through invites", None, "Arrivées par invitation", "Entradas por invitación", "Toetredingen via uitnodigingen",
                      "Beitritte über Einladungen"),
    "counted_joins": ("Counted joins", None, "Arrivées comptées", "Entradas contadas", "Getelde toetredingen", "Gezählte Beitritte"),
    "fake_left_out": ("{count} fake joins left out", None, "{count} arrivées non valides exclues", "{count} entradas no válidas excluidas",
                      "{count} ongeldige toetredingen weggelaten", "{count} ungültige Beitritte ausgelassen"),
    "other": ("Other", None, "Autres", "Otros", "Overig", "Andere"),
    "total": ("Total", None, "Total", "Total", "Totaal", "Gesamt"),
    "share": ("{percent} of the total", None, "{percent} du total", "{percent} del total", "{percent} van het totaal", "{percent} der Summe"),
    "hidden_channel": ("Hidden channel", None, "Salon masqué", "Canal oculto", "Verborgen kanaal", "Verborgener Kanal"),
    "deleted_channel": ("Deleted channel", None, "Salon supprimé", "Canal eliminado", "Verwijderd kanaal", "Gelöschter Kanal"),
    "deleted_suffix": ("{name} (deleted)", None, "{name} (supprimé)", "{name} (eliminado)", "{name} (verwijderd)", "{name} (gelöscht)"),
    "former_member": ("Former member", None, "Ancien membre", "Exmiembro", "Voormalig lid", "Ehemaliges Mitglied"),
    "user_number": ("User {number}", None, "Utilisateur {number}", "Usuario {number}", "Gebruiker {number}", "Nutzer {number}"),
    "unknown_member": ("Unknown member", None, "Membre inconnu", "Miembro desconocido", "Onbekend lid", "Unbekanntes Mitglied"),
    "vanity_link": ("Vanity link", None, "Lien personnalisé", "Enlace personalizado", "Persoonlijke link", "Vanity-Link"),
    "invite_from": ("Invite from {name}", None, "Invitation de {name}", "Invitación de {name}", "Uitnodiging van {name}", "Einladung von {name}"),
    "unknown_invite": ("Unknown invite", None, "Invitation inconnue", "Invitación desconocida", "Onbekende uitnodiging", "Unbekannte Einladung"),
    "nothing_yet": ("Nothing recorded in this period.", None, "Rien d'enregistré sur cette période.", "Nada registrado en este periodo.",
                    "Niets vastgelegd in deze periode.", "In diesem Zeitraum wurde nichts erfasst."),
    "no_activity_data": ("No activities recorded in this period. Activity tracking needs to be on, and the bot needs the Presence intent.", None,
                         "Aucune activité enregistrée sur cette période. Le suivi des activités doit être activé et le bot doit avoir l'intent Presence.",
                         "No hay actividades registradas en este periodo. El seguimiento de actividades debe estar activado y el bot necesita el intent Presence.",
                         "Geen activiteiten vastgelegd in deze periode. Activiteiten bijhouden moet aan staan en de bot heeft de Presence-intent nodig.",
                         "In diesem Zeitraum wurden keine Aktivitäten erfasst. Die Aktivitätserfassung muss an sein, und der Bot braucht den Presence-Intent."),
    "not_ranked": ("Not ranked", None, "Non classé", "Sin puesto", "Niet gerangschikt", "Ohne Rang"),
    "rank_of": ("#{rank} of {total}", None, "{rank}e sur {total}", "{rank}.º de {total}", "{rank}e van {total}", "Platz {rank} von {total}"),
    "rank_first": ("#1 of {total}", None, "1er sur {total}", "1.º de {total}", "1e van {total}", "Platz 1 von {total}"),
    "period_total": ("In this period", None, "Sur la période", "En el periodo", "In deze periode", "Im Zeitraum"),
    "day_1": ("1 day", None, "1 jour", "1 día", "1 dag", "1 Tag"),
    "days_n": ("{days} days", None, "{days} jours", "{days} días", "{days} dagen", "{days} Tage"),
    "last_days": ("Last {days} days", None, "{days} derniers jours", "Últimos {days} días", "Afgelopen {days} dagen", "Letzte {days} Tage"),
    "last_day": ("Last day", None, "Dernières 24 heures", "Último día", "Afgelopen dag", "Letzter Tag"),
    "all_time": ("All time", None, "Depuis le début", "Desde el principio", "Sinds het begin", "Gesamter Zeitraum"),
    "since": ("Since {date}", None, "Depuis le {date}", "Desde el {date}", "Sinds {date}", "Seit {date}"),
    "ranked_count": ("{count} ranked", None, "{count} classés", "{count} en la lista", "{count} in de lijst", "{count} in der Liste"),
    "page_of": ("Page {page} of {pages}", None, "Page {page} sur {pages}", "Página {page} de {pages}", "Pagina {page} van {pages}", "Seite {page} von {pages}"),
    "rank_short": ("#{rank}", None, "#{rank}", "#{rank}", "#{rank}", "#{rank}"),
    "of_total": ("of {total}", None, "sur {total}", "de {total}", "van {total}", "von {total}"),
    "at_a_glance": ("At a glance", None, "En bref", "De un vistazo", "In het kort", "Auf einen Blick"),
    "made_by": ("Captain Guild", None, "Captain Guild", "Captain Guild", "Captain Guild", "Captain Guild"),
    "playing": ("Playing", None, "Joue", "Jugando", "Speelt", "Spielt"),
    "streaming": ("Streaming", None, "En direct", "En directo", "Streamt", "Streamt"),
    "listening": ("Listening", None, "Écoute", "Escuchando", "Luistert", "Hört"),
    "watching": ("Watching", None, "Regarde", "Viendo", "Kijkt", "Schaut"),
    "competing": ("Competing", None, "En compétition", "Compitiendo", "Doet mee aan", "Tritt an"),
    "per_interval_hour": ("per hour", None, "par heure", "por hora", "per uur", "pro Stunde"),
    "per_interval_day": ("per day", None, "par jour", "por día", "per dag", "pro Tag"),
    "per_interval_week": ("per week", None, "par semaine", "por semana", "per week", "pro Woche"),
    "per_interval_month": ("per month", None, "par mois", "por mes", "per maand", "pro Monat"),
    "chart_of": ("{what} {per}", None, "{what} {per}", "{what} {per}", "{what} {per}", "{what} {per}"),
    "members_now": ("Members now", None, "Membres actuels", "Miembros ahora", "Leden nu", "Mitglieder jetzt"),
    "peak": ("Peak {value}", None, "Pic {value}", "Máximo {value}", "Piek {value}", "Spitze {value}"),
    "in_text": ("in messages", None, "dans les messages", "en mensajes", "in berichten", "in Nachrichten"),
    "as_reaction": ("as reactions", None, "en réactions", "como reacciones", "als reacties", "als Reaktionen"),
    "sticker": ("Sticker", None, "Autocollant", "Sticker", "Sticker", "Sticker"),
    # ---- buttons and menus ----
    "refresh": ("Refresh", None, "Actualiser", "Actualizar", "Vernieuwen", "Aktualisieren"),
    "show_chart": ("Chart", None, "Graphique", "Gráfico", "Grafiek", "Diagramm"),
    "show_overview": ("Overview", None, "Vue d'ensemble", "Resumen", "Overzicht", "Übersicht"),
    "period_placeholder": ("Period: {label}", None, "Période : {label}", "Periodo: {label}", "Periode: {label}", "Zeitraum: {label}"),
    "option_server": ("Server setting: {label}", None, "Réglage du serveur : {label}", "Ajuste del servidor: {label}", "Serverinstelling: {label}",
                      "Servereinstellung: {label}"),
    "option_custom": ("Custom...", None, "Personnalisée...", "Personalizado...", "Aangepast...", "Eigener Zeitraum..."),
    "option_custom_hint": ("Days, a date, a timestamp or all", None, "Jours, une date, un horodatage ou tout", "Días, una fecha, una marca de tiempo o todo",
                           "Dagen, een datum, een tijdstempel of alles", "Tage, ein Datum, ein Zeitstempel oder alles"),
    "custom_title": ("Custom period", None, "Période personnalisée", "Periodo personalizado", "Aangepaste periode", "Eigener Zeitraum"),
    "custom_label": ("How far back", None, "Jusqu'où remonter", "Hasta cuándo", "Hoe ver terug", "Wie weit zurück"),
    "custom_placeholder": ("90, 6 months, 2025-03-01, a Discord timestamp or all", None, "90, 6 months, 2025-03-01, un horodatage Discord ou all",
                           "90, 6 months, 2025-03-01, una marca de tiempo de Discord o all", "90, 6 months, 2025-03-01, een Discord-tijdstempel of all",
                           "90, 6 months, 2025-03-01, ein Discord-Zeitstempel oder all"),
    "custom_bad": ("Type a number of days (such as 90), a start date (such as 2025-03-01), a Discord timestamp, or all.", None,
                   "Indiquez un nombre de jours (comme 90), une date de début (comme 2025-03-01), un horodatage Discord ou all.",
                   "Escribe un número de días (como 90), una fecha de inicio (como 2025-03-01), una marca de tiempo de Discord o all.",
                   "Typ een aantal dagen (zoals 90), een begindatum (zoals 2025-03-01), een Discord-tijdstempel of all.",
                   "Gib eine Anzahl Tage (etwa 90), ein Startdatum (etwa 2025-03-01), einen Discord-Zeitstempel oder all ein."),
    "custom_future": ("That date hasn't come yet.", None, "Cette date n'est pas encore arrivée.", "Esa fecha aún no ha llegado.", "Die datum is nog niet geweest.",
                      "Dieses Datum liegt in der Zukunft."),
    "custom_range": ("Pick from 1 to 36,500 days.", None, "Choisissez entre 1 et 36 500 jours.", "Elige entre 1 y 36.500 días.", "Kies van 1 tot 36.500 dagen.",
                     "Wähle 1 bis 36.500 Tage."),
    "custom_too_early": ("Pick a start on or after January 1, 2015. Discord has nothing older.", "Pick a start on or after 1 January 2015. Discord has nothing older.",
                         "Choisissez un début à partir du 1er janvier 2015. Discord n'a rien de plus ancien.",
                         "Elige un inicio a partir del 1 de enero de 2015. Discord no tiene nada anterior.",
                         "Kies een begin op of na 1 januari 2015. Discord heeft niets ouders.", "Wähle einen Beginn ab dem 1. Januar 2015. Ältere Daten gibt es bei Discord nicht."),
    "custom_no_date": ("That date doesn't exist.", None, "Cette date n'existe pas.", "Esa fecha no existe.", "Die datum bestaat niet.", "Dieses Datum gibt es nicht."),
    "first": ("First", None, "Début", "Primera", "Eerste", "Anfang"),
    "previous": ("Previous", None, "Précédente", "Anterior", "Vorige", "Zurück"),
    "next": ("Next", None, "Suivante", "Siguiente", "Volgende", "Weiter"),
    "last": ("Last", None, "Fin", "Última", "Laatste", "Ende"),
    "top_page_placeholder": ("Show: {label}", None, "Afficher : {label}", "Mostrar: {label}", "Toon: {label}", "Anzeigen: {label}"),
    # ---- replies ----
    "off": ("Statistics is switched off in this server. A manager can switch it on from the dashboard.", None,
            "Les statistiques sont désactivées sur ce serveur. Un gestionnaire peut les activer depuis le tableau de bord.",
            "Las estadísticas están desactivadas en este servidor. Un administrador puede activarlas desde el panel.",
            "Statistieken staan uit op deze server. Een beheerder kan ze aanzetten via het dashboard.",
            "Statistiken sind auf diesem Server aus. Ein Verwalter kann sie im Dashboard einschalten."),
    "access_restricted": ("In this server you can only look up your own statistics.", None,
                          "Sur ce serveur, vous ne pouvez consulter que vos propres statistiques.",
                          "En este servidor solo puedes consultar tus propias estadísticas.",
                          "Op deze server kun je alleen je eigen statistieken bekijken.",
                          "Auf diesem Server kannst du nur deine eigenen Statistiken abrufen."),
    "access_self": ("In this server statistics commands only show your own numbers. Try /me.", None,
                    "Sur ce serveur, les commandes de statistiques ne montrent que vos propres chiffres. Essayez /me.",
                    "En este servidor los comandos de estadísticas solo muestran tus propios números. Prueba /me.",
                    "Op deze server tonen statistiekcommando's alleen je eigen cijfers. Probeer /me.",
                    "Auf diesem Server zeigen Statistikbefehle nur deine eigenen Zahlen. Versuch /me."),
    "access_admins": ("In this server only admins can use statistics commands.", None,
                      "Sur ce serveur, seuls les administrateurs peuvent utiliser les commandes de statistiques.",
                      "En este servidor solo los administradores pueden usar los comandos de estadísticas.",
                      "Op deze server kunnen alleen beheerders statistiekcommando's gebruiken.",
                      "Auf diesem Server können nur Admins Statistikbefehle nutzen."),
    "channel_blocked": ("Statistics commands are turned off in this channel.", None, "Les commandes de statistiques sont désactivées dans ce salon.",
                        "Los comandos de estadísticas están desactivados en este canal.", "Statistiekcommando's staan uit in dit kanaal.",
                        "Statistikbefehle sind in diesem Kanal aus."),
    "channel_only": ("Statistics commands work in {channels}.", None, "Les commandes de statistiques fonctionnent dans {channels}.",
                     "Los comandos de estadísticas funcionan en {channels}.", "Statistiekcommando's werken in {channels}.",
                     "Statistikbefehle funktionieren in {channels}."),
    "cant_see_channel": ("You can't see that channel.", None, "Vous ne voyez pas ce salon.", "No puedes ver ese canal.", "Je kunt dat kanaal niet zien.",
                         "Du kannst diesen Kanal nicht sehen."),
    "channel_not_public": ("Not everyone here can see that channel, so its statistics can't go in this channel. Use /channel stats for an answer only you can see.", None,
                           "Tout le monde ici ne voit pas ce salon, ses statistiques ne peuvent donc pas être publiées dans ce salon. Utilisez /channel stats pour une réponse visible par vous seul.",
                           "No todos aquí pueden ver ese canal, así que sus estadísticas no pueden publicarse en este canal. Usa /channel stats para una respuesta que solo tú verás.",
                           "Niet iedereen hier kan dat kanaal zien, dus de statistieken ervan kunnen niet in dit kanaal. Gebruik /channel stats voor een antwoord dat alleen jij ziet.",
                           "Nicht alle hier sehen diesen Kanal, daher können seine Statistiken nicht in diesem Kanal erscheinen. Nutze /channel stats für eine Antwort, die nur du siehst."),
    "not_a_stats_channel": ("That kind of channel has no statistics. Pick a text, voice, forum, stage or category channel.", None,
                            "Ce type de salon n'a pas de statistiques. Choisissez un salon textuel, vocal, forum, de conférence ou une catégorie.",
                            "Ese tipo de canal no tiene estadísticas. Elige un canal de texto, de voz, foro, escenario o una categoría.",
                            "Dat soort kanaal heeft geen statistieken. Kies een tekst-, spraak-, forum- of podiumkanaal of een categorie.",
                            "Diese Art von Kanal hat keine Statistiken. Wähle einen Text-, Sprach-, Forum- oder Bühnenkanal oder eine Kategorie."),
    "private_person": ("This person chose privacy, so no statistics carry their name.", None,
                       "Cette personne a choisi la confidentialité : aucune statistique ne porte son nom.",
                       "Esta persona eligió la privacidad, así que ninguna estadística lleva su nombre.",
                       "Deze persoon heeft voor privacy gekozen, dus er staan geen statistieken op hun naam.",
                       "Diese Person hat Privatsphäre gewählt, daher trägt keine Statistik ihren Namen."),
    "you_private": ("You chose privacy, so no statistics carry your name. /stats privacy changes that.", None,
                    "Vous avez choisi la confidentialité : aucune statistique ne porte votre nom. /stats privacy permet de changer cela.",
                    "Elegiste la privacidad, así que ninguna estadística lleva tu nombre. /stats privacy lo cambia.",
                    "Je hebt voor privacy gekozen, dus er staan geen statistieken op jouw naam. /stats privacy verandert dat.",
                    "Du hast Privatsphäre gewählt, daher trägt keine Statistik deinen Namen. /stats privacy ändert das."),
    "left_server": ("You're not in that server anymore.", None, "Vous n'êtes plus sur ce serveur.", "Ya no estás en ese servidor.", "Je zit niet meer in die server.",
                    "Du bist nicht mehr auf diesem Server."),
    "channel_gone": ("That channel doesn't exist anymore.", None, "Ce salon n'existe plus.", "Ese canal ya no existe.", "Dat kanaal bestaat niet meer.",
                     "Diesen Kanal gibt es nicht mehr."),
    "render_failed": ("That card couldn't be drawn. Try again in a moment.", None, "Cette carte n'a pas pu être dessinée. Réessayez dans un instant.",
                      "No se pudo dibujar esa tarjeta. Inténtalo de nuevo en un momento.", "Die kaart kon niet getekend worden. Probeer het zo opnieuw.",
                      "Diese Karte konnte nicht gezeichnet werden. Versuch es gleich noch einmal."),
    "busy": ("That card is still being drawn. Try again in a moment.", None, "Cette carte est encore en cours de dessin. Réessayez dans un instant.",
             "Esa tarjeta aún se está dibujando. Inténtalo de nuevo en un momento.", "Die kaart wordt nog getekend. Probeer het zo opnieuw.",
             "Diese Karte wird noch gezeichnet. Versuch es gleich noch einmal."),
    "info_help": ("Pick a lookup: activity, emoji, id or roles.", None, "Choisissez une recherche : activity, emoji, id ou roles.",
                  "Elige una consulta: activity, emoji, id o roles.", "Kies een opzoeking: activity, emoji, id of roles.",
                  "Wähle eine Abfrage: activity, emoji, id oder roles."),
    "chart_help": ("Pick a chart: message, voice, member, status, activity, emoji, invite, user or channel.", None,
                   "Choisissez un graphique : message, voice, member, status, activity, emoji, invite, user ou channel.",
                   "Elige un gráfico: message, voice, member, status, activity, emoji, invite, user o channel.",
                   "Kies een grafiek: message, voice, member, status, activity, emoji, invite, user of channel.",
                   "Wähle ein Diagramm: message, voice, member, status, activity, emoji, invite, user oder channel."),
    "dm_failed": ("Your statistics replies are private, and your DMs are closed. Use the slash command for an answer only you can see.", None,
                  "Vos réponses de statistiques sont privées et vos MP sont fermés. Utilisez la commande slash pour une réponse visible par vous seul.",
                  "Tus respuestas de estadísticas son privadas y tus MD están cerrados. Usa el comando de barra para una respuesta que solo tú verás.",
                  "Je statistiekantwoorden zijn privé en je DM's staan dicht. Gebruik het slash-commando voor een antwoord dat alleen jij ziet.",
                  "Deine Statistikantworten sind privat, und deine DMs sind zu. Nutze den Slash-Befehl für eine Antwort, die nur du siehst."),
    "dm_closed": ("This answer names the server's settings, so it goes by DM, and your DMs are closed. Use the slash command for an answer only you can see.", None,
                  "Cette réponse cite les réglages du serveur, elle part donc en MP, et vos MP sont fermés. Utilisez la commande slash pour une réponse visible par vous seul.",
                  "Esta respuesta muestra los ajustes del servidor, así que va por MD, y tus MD están cerrados. Usa el comando de barra para una respuesta que solo tú verás.",
                  "Dit antwoord noemt de instellingen van de server en gaat daarom via DM, maar je DM's staan dicht. Gebruik het slash-commando voor een antwoord dat alleen jij ziet.",
                  "Diese Antwort nennt die Einstellungen des Servers und kommt deshalb per DM, aber deine DMs sind geschlossen. Nutze den Slash-Befehl für eine Antwort, die nur du siehst."),
    "sent_by_dm": ("Sent to your DMs.", None, "Envoyé en MP.", "Enviado por MD.", "Gestuurd via DM.", "Per DM geschickt."),
    # ---- privacy, language and private replies ----
    "privacy_title": ("Statistics privacy", None, "Confidentialité des statistiques", "Privacidad de las estadísticas", "Privacy van statistieken",
                      "Privatsphäre bei Statistiken"),
    "privacy_off_text": (
        "Turning privacy on takes your name off your statistics in every server this bot is in, back to the start and from now on. Your messages, "
        "voice time, activities and emoji still count toward each server's totals, under no name. Rankings, cards and stat roles leave you out.\n\n"
        "It can't be undone for what's already recorded: once your counts are merged, nothing can tell which part was yours. You can change this once "
        "every 24 hours.", None,
        "Activer la confidentialité retire votre nom de vos statistiques sur tous les serveurs où se trouve ce bot, pour le passé comme pour la suite. "
        "Vos messages, votre temps en vocal, vos activités et vos émojis comptent toujours dans les totaux de chaque serveur, sans nom. Les classements, "
        "les cartes et les rôles statistiques vous laissent de côté.\n\nCe qui est déjà enregistré ne peut pas être rétabli : une fois vos chiffres "
        "fusionnés, plus rien ne dit quelle part était la vôtre. Vous pouvez changer ce réglage une fois toutes les 24 heures.",
        "Activar la privacidad quita tu nombre de tus estadísticas en todos los servidores donde está este bot, hacia atrás y desde ahora. Tus mensajes, "
        "tu tiempo de voz, tus actividades y tus emojis siguen contando en los totales de cada servidor, sin nombre. Las clasificaciones, las tarjetas y "
        "los roles por estadísticas te dejan fuera.\n\nLo que ya está registrado no se puede deshacer: una vez fusionados tus números, nada indica qué "
        "parte era tuya. Puedes cambiarlo una vez cada 24 horas.",
        "Als je privacy aanzet, verdwijnt je naam uit je statistieken op elke server waar deze bot zit, terug tot het begin en vanaf nu. Je berichten, "
        "spraaktijd, activiteiten en emoji tellen nog steeds mee in de totalen van elke server, zonder naam. Ranglijsten, kaarten en statistiekrollen "
        "laten je weg.\n\nWat al is vastgelegd kan niet worden teruggedraaid: zodra je cijfers zijn samengevoegd, valt niet meer te zien welk deel van "
        "jou was. Je kunt dit eens per 24 uur veranderen.",
        "Wenn du Privatsphäre einschaltest, verschwindet dein Name aus deinen Statistiken auf allen Servern mit diesem Bot, rückwirkend und ab jetzt. "
        "Deine Nachrichten, Sprachzeit, Aktivitäten und Emojis zählen weiter zu den Summen jedes Servers, ohne Namen. Bestenlisten, Karten und "
        "Statistikrollen lassen dich aus.\n\nWas schon erfasst ist, lässt sich nicht zurückholen: Sind deine Zahlen einmal zusammengeführt, lässt "
        "sich nicht mehr sagen, welcher Teil deiner war. Du kannst das einmal alle 24 Stunden ändern."),
    "privacy_on_text": (
        "Privacy is on, so no statistics carry your name.\n\nTurning it off puts your name on statistics recorded from now on. What was recorded while "
        "privacy was on stays without a name. You can change this once every 24 hours.", None,
        "La confidentialité est activée : aucune statistique ne porte votre nom.\n\nLa désactiver remet votre nom sur les statistiques enregistrées à "
        "partir de maintenant. Ce qui a été enregistré pendant la confidentialité reste sans nom. Vous pouvez changer ce réglage une fois toutes les 24 heures.",
        "La privacidad está activada, así que ninguna estadística lleva tu nombre.\n\nDesactivarla pone tu nombre en las estadísticas que se registren "
        "desde ahora. Lo registrado mientras estaba activada sigue sin nombre. Puedes cambiarlo una vez cada 24 horas.",
        "Privacy staat aan, dus er staan geen statistieken op jouw naam.\n\nAls je het uitzet, komt je naam op statistieken die vanaf nu worden "
        "vastgelegd. Wat is vastgelegd terwijl privacy aan stond, blijft zonder naam. Je kunt dit eens per 24 uur veranderen.",
        "Privatsphäre ist an, daher trägt keine Statistik deinen Namen.\n\nWenn du sie ausschaltest, steht dein Name auf allem, was ab jetzt erfasst "
        "wird. Was während der Privatsphäre erfasst wurde, bleibt ohne Namen. Du kannst das einmal alle 24 Stunden ändern."),
    "privacy_turn_on": ("Turn privacy on", None, "Activer la confidentialité", "Activar la privacidad", "Privacy aanzetten", "Privatsphäre einschalten"),
    "privacy_turn_off": ("Turn privacy off", None, "Désactiver la confidentialité", "Desactivar la privacidad", "Privacy uitzetten", "Privatsphäre ausschalten"),
    "cancel": ("Cancel", None, "Annuler", "Cancelar", "Annuleren", "Abbrechen"),
    "canceled": ("Nothing changed.", None, "Rien n'a changé.", "No ha cambiado nada.", "Er is niets veranderd.", "Nichts wurde geändert."),
    "privacy_working": ("Taking your name off your statistics. This can take a minute.", None,
                        "Retrait de votre nom de vos statistiques. Cela peut prendre une minute.",
                        "Quitando tu nombre de tus estadísticas. Puede tardar un minuto.",
                        "Je naam wordt uit je statistieken gehaald. Dit kan een minuut duren.",
                        "Dein Name wird aus deinen Statistiken entfernt. Das kann eine Minute dauern."),
    "privacy_done_on": ("Done. Your statistics carry no name now, in every server.", None,
                        "C'est fait. Vos statistiques ne portent plus de nom, sur tous les serveurs.",
                        "Hecho. Tus estadísticas ya no llevan nombre, en ningún servidor.",
                        "Klaar. Je statistieken staan nu op geen enkele naam, op elke server.",
                        "Erledigt. Deine Statistiken tragen jetzt auf keinem Server mehr einen Namen."),
    "privacy_done_off": ("Done. Statistics recorded from now on carry your name again.", None,
                         "C'est fait. Les statistiques enregistrées à partir de maintenant portent de nouveau votre nom.",
                         "Hecho. Las estadísticas que se registren desde ahora vuelven a llevar tu nombre.",
                         "Klaar. Statistieken die vanaf nu worden vastgelegd staan weer op jouw naam.",
                         "Erledigt. Was ab jetzt erfasst wird, trägt wieder deinen Namen."),
    "privacy_wait": ("You changed this <t:{changed}:R>. You can change it again <t:{next}:R>.", None,
                     "Vous avez changé ce réglage <t:{changed}:R>. Vous pourrez le changer de nouveau <t:{next}:R>.",
                     "Lo cambiaste <t:{changed}:R>. Podrás cambiarlo de nuevo <t:{next}:R>.",
                     "Je hebt dit <t:{changed}:R> veranderd. Je kunt het <t:{next}:R> weer veranderen.",
                     "Du hast das <t:{changed}:R> geändert. Du kannst es <t:{next}:R> wieder ändern."),
    "privacy_failed": ("Your name couldn't be taken off yet. Privacy is on for new statistics, and the rest is tried again when the bot restarts.", None,
                       "Votre nom n'a pas encore pu être retiré. La confidentialité s'applique aux nouvelles statistiques, et le reste sera retenté au redémarrage du bot.",
                       "Aún no se pudo quitar tu nombre. La privacidad se aplica a las estadísticas nuevas, y el resto se volverá a intentar cuando el bot se reinicie.",
                       "Je naam kon nog niet worden weggehaald. Privacy geldt voor nieuwe statistieken, en de rest wordt opnieuw geprobeerd als de bot herstart.",
                       "Dein Name konnte noch nicht entfernt werden. Privatsphäre gilt für neue Statistiken, der Rest wird beim Neustart des Bots erneut versucht."),
    "language_set": ("Your statistics cards and replies now use {name}.", None, "Vos cartes et réponses de statistiques sont maintenant en {name}.",
                     "Tus tarjetas y respuestas de estadísticas ahora usan {name}.", "Je statistiekkaarten en -antwoorden zijn nu in het {name}.",
                     "Deine Statistikkarten und -antworten sind jetzt auf {name}."),
    "language_auto": ("Your statistics cards and replies now follow your Discord language.", None,
                      "Vos cartes et réponses de statistiques suivent maintenant la langue de Discord.",
                      "Tus tarjetas y respuestas de estadísticas ahora siguen el idioma de Discord.",
                      "Je statistiekkaarten en -antwoorden volgen nu je Discord-taal.",
                      "Deine Statistikkarten und -antworten folgen jetzt deiner Discord-Sprache."),
    "language_now": ("Your statistics cards and replies use {name}. Pick another language or follow Discord's.", None,
                     "Vos cartes et réponses de statistiques sont en {name}. Choisissez une autre langue ou suivez celle de Discord.",
                     "Tus tarjetas y respuestas de estadísticas usan {name}. Elige otro idioma o sigue el de Discord.",
                     "Je statistiekkaarten en -antwoorden zijn in het {name}. Kies een andere taal of volg die van Discord.",
                     "Deine Statistikkarten und -antworten sind auf {name}. Wähle eine andere Sprache oder folge Discord."),
    "private_replies_on": ("Your statistics replies now come back only to you. Typed commands answer by DM.", None,
                           "Vos réponses de statistiques ne sont maintenant visibles que par vous. Les commandes tapées répondent en MP.",
                           "Tus respuestas de estadísticas ahora solo las ves tú. Los comandos escritos responden por MD.",
                           "Je statistiekantwoorden zijn nu alleen voor jou zichtbaar. Getypte commando's antwoorden via DM.",
                           "Deine Statistikantworten siehst jetzt nur du. Getippte Befehle antworten per DM."),
    "private_replies_off": ("Your statistics replies show in the channel again.", None, "Vos réponses de statistiques s'affichent de nouveau dans le salon.",
                            "Tus respuestas de estadísticas vuelven a mostrarse en el canal.", "Je statistiekantwoorden verschijnen weer in het kanaal.",
                            "Deine Statistikantworten erscheinen wieder im Kanal."),
    "private_replies_now_on": ("Your statistics replies come back only to you.", None, "Vos réponses de statistiques ne sont visibles que par vous.",
                               "Tus respuestas de estadísticas solo las ves tú.", "Je statistiekantwoorden zijn alleen voor jou zichtbaar.",
                               "Deine Statistikantworten siehst nur du."),
    "private_replies_now_off": ("Your statistics replies show in the channel.", None, "Vos réponses de statistiques s'affichent dans le salon.",
                                "Tus respuestas de estadísticas se muestran en el canal.", "Je statistiekantwoorden verschijnen in het kanaal.",
                                "Deine Statistikantworten erscheinen im Kanal."),
    # ---- /info ----
    "info_no_presence": ("The bot can't see presences, so it can't read what anyone is doing. That needs the Presence intent.", None,
                         "Le bot ne voit pas les présences, il ne peut donc pas lire ce que fait chacun. Il faut l'intent Presence.",
                         "El bot no ve las presencias, así que no puede leer lo que hace nadie. Hace falta el intent Presence.",
                         "De bot ziet geen aanwezigheid, dus kan niet zien wat iemand doet. Daarvoor is de Presence-intent nodig.",
                         "Der Bot sieht keine Präsenzen und kann daher nicht lesen, was jemand macht. Dafür braucht er den Presence-Intent."),
    "info_no_activity": ("{name} isn't showing any activity right now. Activities show only when the member shares them in their Discord settings.", None,
                         "{name} n'affiche aucune activité pour l'instant. Les activités ne s'affichent que si le membre les partage dans ses réglages Discord.",
                         "{name} no muestra ninguna actividad ahora mismo. Las actividades solo se ven si el miembro las comparte en sus ajustes de Discord.",
                         "{name} toont nu geen activiteit. Activiteiten zijn alleen te zien als het lid ze deelt in de Discord-instellingen.",
                         "{name} zeigt gerade keine Aktivität. Aktivitäten erscheinen nur, wenn das Mitglied sie in seinen Discord-Einstellungen teilt."),
    "info_activity_title": ("What {name} is doing now", None, "Ce que fait {name}", "Lo que hace {name} ahora", "Wat {name} nu doet", "Was {name} gerade macht"),
    "info_counted": ("Counted", None, "Comptée", "Se cuenta", "Wordt geteld", "Wird gezählt"),
    "info_not_counted_off": ("Not counted: activity tracking is off here", None, "Non comptée : le suivi des activités est désactivé ici",
                             "No se cuenta: el seguimiento de actividades está desactivado aquí", "Niet geteld: activiteiten bijhouden staat hier uit",
                             "Nicht gezählt: Aktivitätserfassung ist hier aus"),
    "info_not_counted_unverified": ("Not counted: Discord gives no app ID to verify it", None, "Non comptée : Discord ne donne pas d'ID d'appli pour la vérifier",
                                    "No se cuenta: Discord no da un ID de app para verificarla", "Niet geteld: Discord geeft geen app-ID om het te controleren",
                                    "Nicht gezählt: Discord liefert keine App-ID zur Prüfung"),
    "info_not_counted_kind": ("Not counted: a custom status isn't an activity", None, "Non comptée : un statut personnalisé n'est pas une activité",
                              "No se cuenta: un estado personalizado no es una actividad", "Niet geteld: een eigen status is geen activiteit",
                              "Nicht gezählt: Ein eigener Status ist keine Aktivität"),
    "info_app_id": ("App ID {id}", None, "ID d'appli {id}", "ID de app {id}", "App-ID {id}", "App-ID {id}"),
    "info_no_app_id": ("No app ID", None, "Pas d'ID d'appli", "Sin ID de app", "Geen app-ID", "Keine App-ID"),
    "info_server_id": ("Server", None, "Serveur", "Servidor", "Server", "Server"),
    "info_ids_title": ("IDs", None, "Identifiants", "ID", "ID's", "IDs"),
    "info_ids_none": ("Mention people, channels or roles, or paste their IDs, to see their IDs too.", None,
                      "Mentionnez des personnes, des salons ou des rôles, ou collez leurs identifiants, pour voir aussi leurs identifiants.",
                      "Menciona personas, canales o roles, o pega sus ID, para ver también sus ID.",
                      "Noem mensen, kanalen of rollen, of plak hun ID's, om ook hun ID's te zien.",
                      "Erwähne Personen, Kanäle oder Rollen oder füge ihre IDs ein, um auch ihre IDs zu sehen."),
    "info_roles_none": ("Nobody holds {roles}.", None, "Personne n'a {roles}.", "Nadie tiene {roles}.", "Niemand heeft {roles}.", "Niemand hat {roles}."),
    "info_roles_done": ("{count} members hold {roles}. The file lists them.", None, "{count} membres ont {roles}. Le fichier les liste.",
                        "{count} miembros tienen {roles}. El archivo los enumera.", "{count} leden hebben {roles}. Het bestand somt ze op.",
                        "{count} Mitglieder haben {roles}. Die Datei listet sie auf."),
    "info_roles_one": ("1 member holds {roles}. The file lists them.", None, "1 membre a {roles}. Le fichier le liste.", "1 miembro tiene {roles}. El archivo lo incluye.",
                       "1 lid heeft {roles}. Het bestand noemt het.", "1 Mitglied hat {roles}. Die Datei nennt es."),
    "info_roles_need": ("Name at least one role.", None, "Indiquez au moins un rôle.", "Indica al menos un rol.", "Noem minstens één rol.", "Nenne mindestens eine Rolle."),
    "info_emoji_title": ("Server emoji and stickers by use", None, "Émojis et autocollants du serveur par utilisation", "Emojis y stickers del servidor por uso",
                         "Emoji en stickers van de server op gebruik", "Emojis und Sticker des Servers nach Nutzung"),
    "info_emoji_none": ("This server has no emoji or stickers of its own.", None, "Ce serveur n'a ni émojis ni autocollants à lui.",
                        "Este servidor no tiene emojis ni stickers propios.", "Deze server heeft geen eigen emoji of stickers.",
                        "Dieser Server hat keine eigenen Emojis oder Sticker."),
    "info_emoji_more": ("{count} more in the file.", None, "{count} de plus dans le fichier.", "{count} más en el archivo.", "Nog {count} in het bestand.",
                        "{count} weitere in der Datei."),
    "unused": ("unused", None, "jamais utilisé", "sin usar", "ongebruikt", "unbenutzt"),
    # ---- settings and filters ----
    "saved": ("Saved.", None, "Enregistré.", "Guardado.", "Opgeslagen.", "Gespeichert."),
    "settings_title": ("Statistics settings", None, "Réglages des statistiques", "Ajustes de estadísticas", "Statistiekinstellingen", "Statistik-Einstellungen"),
    "settings_time": ("Time", None, "Temps", "Tiempo", "Tijd", "Zeit"),
    "settings_view": ("What views show", None, "Ce que montrent les vues", "Qué muestran las vistas", "Wat de weergaven tonen", "Was die Ansichten zeigen"),
    "settings_access": ("Who can use the commands", None, "Qui peut utiliser les commandes", "Quién puede usar los comandos", "Wie de commando's mag gebruiken",
                        "Wer die Befehle nutzen kann"),
    "settings_recorded": ("What's recorded", None, "Ce qui est enregistré", "Qué se registra", "Wat wordt vastgelegd", "Was erfasst wird"),
    "settings_more": ("Everything else is on the dashboard: {link}", None, "Tout le reste est sur le tableau de bord : {link}", "Todo lo demás está en el panel: {link}",
                      "De rest staat op het dashboard: {link}", "Alles andere ist im Dashboard: {link}"),
    "set_zone": ("Time zone: {value}", None, "Fuseau horaire : {value}", "Zona horaria: {value}", "Tijdzone: {value}", "Zeitzone: {value}"),
    "set_lookback": ("Period: {value}", None, "Période : {value}", "Periodo: {value}", "Periode: {value}", "Zeitraum: {value}"),
    "set_interval": ("Chart interval: {value}", None, "Intervalle des graphiques : {value}", "Intervalo de los gráficos: {value}", "Grafiekinterval: {value}",
                     "Diagrammintervall: {value}"),
    "set_unit": ("Voice time in: {value}", None, "Temps vocal en : {value}", "Tiempo de voz en: {value}", "Spraaktijd in: {value}", "Sprachzeit in: {value}"),
    "set_include": ("Counts: {value}", None, "Comptés : {value}", "Se cuentan: {value}", "Telt: {value}", "Gezählt: {value}"),
    "set_states": ("Voice states: {value}", None, "États vocaux : {value}", "Estados de voz: {value}", "Spraakstatussen: {value}", "Sprachstatus: {value}"),
    "set_channels": ("Channels: {value}", None, "Salons : {value}", "Canales: {value}", "Kanalen: {value}", "Kanäle: {value}"),
    "set_voice_channels": ("Voice channels: {value}", None, "Salons vocaux : {value}", "Canales de voz: {value}", "Spraakkanalen: {value}", "Sprachkanäle: {value}"),
    "set_members": ("Members: {value}", None, "Membres : {value}", "Miembros: {value}", "Leden: {value}", "Mitglieder: {value}"),
    "set_roles_allow": ("Only members with: {value}", None, "Seulement les membres avec : {value}", "Solo miembros con: {value}", "Alleen leden met: {value}",
                        "Nur Mitglieder mit: {value}"),
    "set_roles_block": ("Leaving out members with: {value}", None, "Sans les membres avec : {value}", "Sin los miembros con: {value}", "Zonder leden met: {value}",
                        "Ohne Mitglieder mit: {value}"),
    "set_emoji": ("Emoji: {value}", None, "Émojis : {value}", "Emojis: {value}", "Emoji: {value}", "Emojis: {value}"),
    "set_access": ("Access: {value}", None, "Accès : {value}", "Acceso: {value}", "Toegang: {value}", "Zugriff: {value}"),
    "set_command_channels": ("Command channels: {value}", None, "Salons de commandes : {value}", "Canales de comandos: {value}", "Commandokanalen: {value}",
                             "Befehlskanäle: {value}"),
    "set_override": ("Roles that skip these rules: {value}", None, "Rôles qui ignorent ces règles : {value}", "Roles que se saltan estas reglas: {value}",
                     "Rollen die deze regels overslaan: {value}", "Rollen, die diese Regeln umgehen: {value}"),
    "settings_invites": ("Invites", None, "Invitations", "Invitaciones", "Uitnodigingen", "Einladungen"),
    "set_invite_account": ("A join counts once the account is {value} old", None, "Une arrivée compte si le compte a {value}",
                           "Una entrada cuenta si la cuenta tiene {value}", "Een toetreding telt als het account {value} oud is",
                           "Ein Beitritt zählt, wenn das Konto {value} alt ist"),
    "set_invite_join": ("A join counts once the member has stayed {value}", None, "Une arrivée compte si le membre est resté {value}",
                        "Una entrada cuenta si el miembro se ha quedado {value}", "Een toetreding telt als het lid {value} is gebleven",
                        "Ein Beitritt zählt, wenn das Mitglied {value} geblieben ist"),
    "set_invite_roles": ("Roles a join needs ({match}): {value}", None, "Rôles requis ({match}) : {value}", "Roles necesarios ({match}): {value}",
                         "Vereiste rollen ({match}): {value}", "Nötige Rollen ({match}): {value}"),
    "match_any": ("any of them", None, "l'un d'eux", "cualquiera", "een ervan", "eine davon"),
    "match_all": ("all of them", None, "tous", "todos", "allemaal", "alle"),
    "no_minimum": ("any age", None, "n'importe quel âge", "cualquier edad", "elke leeftijd", "beliebig lange"),
    "set_verify": ("Activities need an app ID Discord can check: {value}", None, "Les activités doivent avoir un ID d'appli vérifiable : {value}",
                   "Las actividades necesitan un ID de app verificable: {value}", "Activiteiten hebben een controleerbare app-ID nodig: {value}",
                   "Aktivitäten brauchen eine prüfbare App-ID: {value}"),
    "set_activities": ("Activity tracking: {value}", None, "Suivi des activités : {value}", "Seguimiento de actividades: {value}", "Activiteiten bijhouden: {value}",
                       "Aktivitätserfassung: {value}"),
    "set_untracked": ("Not recorded: {value}", None, "Non enregistrés : {value}", "No se registran: {value}", "Niet vastgelegd: {value}", "Nicht erfasst: {value}"),
    "on": ("on", None, "activé", "activado", "aan", "an"),
    "off_word": ("off", None, "désactivé", "desactivado", "uit", "aus"),
    "none": ("none", None, "aucun", "ninguno", "geen", "keine"),
    "everything": ("everything", None, "tout", "todo", "alles", "alles"),
    "all_channels": ("all", None, "tous", "todos", "alle", "alle"),
    "only_list": ("only {items}", None, "seulement {items}", "solo {items}", "alleen {items}", "nur {items}"),
    "all_but_list": ("all but {items}", None, "tous sauf {items}", "todos menos {items}", "alle behalve {items}", "alle außer {items}"),
    "same_as_text": ("same as text channels", None, "comme les salons textuels", "igual que los canales de texto", "zoals de tekstkanalen", "wie die Textkanäle"),
    "and_more": ("{items} and {count} more", None, "{items} et {count} de plus", "{items} y {count} más", "{items} en nog {count}", "{items} und {count} weitere"),
    "hidden_from_you": ("{amount} hidden from you", None, "{amount} que vous ne voyez pas", "{amount} que no puedes ver", "{amount} voor jou verborgen",
                        "{amount} für dich verborgen"),
    "include_humans": ("people", None, "personnes", "personas", "mensen", "Personen"),
    "include_bots": ("bots", None, "bots", "bots", "bots", "Bots"),
    "include_both": ("people and bots", None, "personnes et bots", "personas y bots", "mensen en bots", "Personen und Bots"),
    "interval_hour": ("hour", None, "heure", "hora", "uur", "Stunde"),
    "interval_day": ("day", None, "jour", "día", "dag", "Tag"),
    "interval_week": ("week", None, "semaine", "semana", "week", "Woche"),
    "interval_month": ("month", None, "mois", "mes", "maand", "Monat"),
    "state_talking": ("talking", None, "parle", "hablando", "praat", "spricht"),
    "state_afk": ("AFK", None, "AFK", "AFK", "AFK", "AFK"),
    "state_self_muted": ("muted", None, "micro coupé", "silenciado", "gedempt", "stumm"),
    "state_self_deafened": ("deafened", None, "son coupé", "ensordecido", "doof gezet", "taub"),
    "state_server_muted": ("muted by staff", None, "rendu muet par l'équipe", "silenciado por el equipo", "gedempt door staf", "vom Team stumm"),
    "state_server_deafened": ("deafened by staff", None, "son coupé par l'équipe", "ensordecido por el equipo", "doof gezet door staf", "vom Team taub"),
    "access_all": ("everyone", None, "tout le monde", "todos", "iedereen", "alle"),
    "access_restricted_word": ("everyone, about themselves", None, "chacun, sur soi-même", "cada uno, sobre sí mismo", "iedereen, over zichzelf", "alle, über sich selbst"),
    "access_self_word": ("only /me-style commands", None, "seulement les commandes comme /me", "solo comandos como /me", "alleen commando's zoals /me",
                         "nur Befehle wie /me"),
    "access_admins_word": ("admins only", None, "administrateurs seulement", "solo administradores", "alleen beheerders", "nur Admins"),
    "only_admins_change": ("Only the server owner or an Administrator can change the roles that skip these rules.", None,
                           "Seuls le propriétaire du serveur ou un administrateur peuvent changer les rôles qui ignorent ces règles.",
                           "Solo el dueño del servidor o un administrador pueden cambiar los roles que se saltan estas reglas.",
                           "Alleen de servereigenaar of een beheerder kan de rollen veranderen die deze regels overslaan.",
                           "Nur der Serverbesitzer oder ein Administrator kann die Rollen ändern, die diese Regeln umgehen."),
    "bad_zone": ("Pick a time zone from the list, such as America/Los_Angeles or UTC.", None,
                 "Choisissez un fuseau horaire dans la liste, comme Europe/Paris ou UTC.",
                 "Elige una zona horaria de la lista, como Europe/Madrid o UTC.",
                 "Kies een tijdzone uit de lijst, zoals Europe/Amsterdam of UTC.",
                 "Wähle eine Zeitzone aus der Liste, etwa Europe/Berlin oder UTC."),
    "bad_states": ("Name voice states from this list, or all: {states}.", None, "Indiquez des états vocaux de cette liste, ou all : {states}.",
                   "Indica estados de voz de esta lista, o all: {states}.", "Noem spraakstatussen uit deze lijst, of all: {states}.",
                   "Nenne Sprachstatus aus dieser Liste oder all: {states}."),
    "nothing_to_change": ("Pick something to change. /stats_settings show lists everything.", None,
                          "Choisissez quelque chose à changer. /stats_settings show liste tout.",
                          "Elige algo que cambiar. /stats_settings show lo muestra todo.",
                          "Kies iets om te veranderen. /stats_settings show toont alles.",
                          "Wähle etwas zum Ändern. /stats_settings show zeigt alles."),
    "filter_added": ("Added {item} to the {list}.", None, "{item} ajouté à la {list}.", "{item} añadido a la {list}.", "{item} toegevoegd aan de {list}.",
                     "{item} zur {list} hinzugefügt."),
    "filter_removed": ("Took {item} off the {list}.", None, "{item} retiré de la {list}.", "{item} quitado de la {list}.", "{item} van de {list} gehaald.",
                       "{item} von der {list} entfernt."),
    "thread_means_channel": ("Threads count as their channel, so this uses {channel}.", None, "Les fils comptent comme leur salon, cela vaut donc pour {channel}.",
                             "Los hilos cuentan como su canal, así que se usa {channel}.", "Threads tellen als hun kanaal, dus dit gebruikt {channel}.",
                             "Threads zählen als ihr Kanal, daher gilt das für {channel}."),
    "filter_already": ("{item} is already on the {list}.", None, "{item} est déjà dans la {list}.", "{item} ya está en la {list}.", "{item} staat al op de {list}.",
                       "{item} steht schon auf der {list}."),
    "filter_missing": ("{item} isn't on the {list}.", None, "{item} n'est pas dans la {list}.", "{item} no está en la {list}.", "{item} staat niet op de {list}.",
                       "{item} steht nicht auf der {list}."),
    "filter_full": ("The {list} holds {limit} at most.", None, "La {list} en contient {limit} au plus.", "La {list} admite como máximo {limit}.",
                    "De {list} bevat er hoogstens {limit}.", "Die {list} fasst höchstens {limit}."),
    "list_channels": ("channel filter", None, "liste des salons", "lista de canales", "kanaalfilter", "Kanalliste"),
    "list_members": ("member filter", None, "liste des membres", "lista de miembros", "ledenfilter", "Mitgliederliste"),
    "list_roles_allow": ("list of roles members need", None, "liste des rôles requis", "lista de roles necesarios", "lijst met vereiste rollen",
                         "Liste der nötigen Rollen"),
    "list_roles_block": ("list of roles left out", None, "liste des rôles exclus", "lista de roles excluidos", "lijst met uitgesloten rollen",
                         "Liste der ausgeschlossenen Rollen"),
    "list_emoji": ("emoji filter", None, "liste des émojis", "lista de emojis", "emojifilter", "Emojiliste"),
    "mode_channels_block": ("The channels on the list are left out of statistics views.", None,
                            "Les salons de la liste sont exclus des vues de statistiques.", "Los canales de la lista se excluyen de las vistas de estadísticas.",
                            "De kanalen op de lijst worden weggelaten uit statistiekweergaven.", "Die Kanäle auf der Liste fehlen in den Statistikansichten."),
    "mode_channels_allow": ("Statistics views show only the channels on the list.", None, "Les vues de statistiques ne montrent que les salons de la liste.",
                            "Las vistas de estadísticas solo muestran los canales de la lista.", "Statistiekweergaven tonen alleen de kanalen op de lijst.",
                            "Statistikansichten zeigen nur die Kanäle auf der Liste."),
    "mode_members_block": ("The members on the list are left out of statistics views.", None,
                           "Les membres de la liste sont exclus des vues de statistiques.", "Los miembros de la lista se excluyen de las vistas de estadísticas.",
                           "De leden op de lijst worden weggelaten uit statistiekweergaven.", "Die Mitglieder auf der Liste fehlen in den Statistikansichten."),
    "mode_members_allow": ("Statistics views show only the members on the list.", None, "Les vues de statistiques ne montrent que les membres de la liste.",
                           "Las vistas de estadísticas solo muestran los miembros de la lista.", "Statistiekweergaven tonen alleen de leden op de lijst.",
                           "Statistikansichten zeigen nur die Mitglieder auf der Liste."),
    "mode_emoji_block": ("The emoji on the list are left out of statistics views.", None, "Les émojis de la liste sont exclus des vues de statistiques.",
                         "Los emojis de la lista se excluyen de las vistas de estadísticas.", "De emoji op de lijst worden weggelaten uit statistiekweergaven.",
                         "Die Emojis auf der Liste fehlen in den Statistikansichten."),
    "mode_emoji_allow": ("Statistics views show only the emoji on the list.", None, "Les vues de statistiques ne montrent que les émojis de la liste.",
                         "Las vistas de estadísticas solo muestran los emojis de la lista.", "Statistiekweergaven tonen alleen de emoji op de lijst.",
                         "Statistikansichten zeigen nur die Emojis auf der Liste."),
    "bad_emoji": ("That isn't an emoji this server can count. Use a standard emoji, one of this server's emoji, or its ID.", None,
                  "Ce n'est pas un émoji que ce serveur peut compter. Utilisez un émoji standard, un émoji de ce serveur ou son identifiant.",
                  "Eso no es un emoji que este servidor pueda contar. Usa un emoji estándar, uno de este servidor o su ID.",
                  "Dat is geen emoji die deze server kan tellen. Gebruik een standaardemoji, een emoji van deze server of de ID ervan.",
                  "Das ist kein Emoji, das dieser Server zählen kann. Nimm ein Standard-Emoji, ein Emoji dieses Servers oder seine ID."),
    # ---- /stats_check ----
    "check_title": ("Statistics check", None, "Vérification des statistiques", "Revisión de estadísticas", "Statistiekcontrole", "Statistik-Prüfung"),
    "check_ok": ("Everything statistics needs is in place.", None, "Tout ce dont les statistiques ont besoin est en place.",
                 "Todo lo que necesitan las estadísticas está en orden.", "Alles wat statistieken nodig hebben is in orde.",
                 "Alles, was die Statistik braucht, ist vorhanden."),
    "check_off": ("Statistics is switched off, so nothing is being recorded.", None, "Les statistiques sont désactivées : rien n'est enregistré.",
                  "Las estadísticas están desactivadas, así que no se registra nada.", "Statistieken staan uit, dus er wordt niets vastgelegd.",
                  "Statistiken sind aus, daher wird nichts erfasst."),
    "check_presence": ("Statuses and activities: the bot has no Presence intent. Statuses come from Discord's count of members online, with no breakdown, and activities aren't recorded.", None,
                       "Statuts et activités : le bot n'a pas l'intent Presence. Les statuts viennent du compte Discord des membres en ligne, sans détail, et les activités ne sont pas enregistrées.",
                       "Estados y actividades: el bot no tiene el intent Presence. Los estados salen del recuento de Discord de miembros conectados, sin desglose, y las actividades no se registran.",
                       "Statussen en activiteiten: de bot heeft de Presence-intent niet. Statussen komen uit Discords telling van leden online, zonder uitsplitsing, en activiteiten worden niet vastgelegd.",
                       "Status und Aktivitäten: Der Bot hat den Presence-Intent nicht. Status stammen aus Discords Zählung der Mitglieder online, ohne Aufteilung, und Aktivitäten werden nicht erfasst."),
    "check_members_intent": ("Member counts, joins and role filters: the bot has no Server Members intent.", None,
                             "Nombre de membres, arrivées et filtres de rôles : le bot n'a pas l'intent Server Members.",
                             "Recuento de miembros, entradas y filtros de roles: el bot no tiene el intent Server Members.",
                             "Ledenaantallen, toetredingen en rolfilters: de bot heeft de Server Members-intent niet.",
                             "Mitgliederzahl, Beitritte und Rollenfilter: Der Bot hat den Server-Members-Intent nicht."),
    "check_content_intent": ("Emoji in messages and typed commands: the bot has no Message Content intent.", None,
                             "Émojis dans les messages et commandes tapées : le bot n'a pas l'intent Message Content.",
                             "Emojis en los mensajes y comandos escritos: el bot no tiene el intent Message Content.",
                             "Emoji in berichten en getypte commando's: de bot heeft de Message Content-intent niet.",
                             "Emojis in Nachrichten und getippte Befehle: Der Bot hat den Message-Content-Intent nicht."),
    "check_activities_off": ("Activities: tracking is off. A manager can switch it on on the dashboard.", None,
                             "Activités : le suivi est désactivé. Un gestionnaire peut l'activer sur le tableau de bord.",
                             "Actividades: el seguimiento está desactivado. Un administrador puede activarlo en el panel.",
                             "Activiteiten: bijhouden staat uit. Een beheerder kan het aanzetten op het dashboard.",
                             "Aktivitäten: Die Erfassung ist aus. Ein Verwalter kann sie im Dashboard einschalten."),
    "check_invites": ("Invites: the bot needs Manage Server to see which invite each member used.", None,
                      "Invitations : le bot a besoin de Gérer le serveur pour voir quelle invitation chaque membre a utilisée.",
                      "Invitaciones: el bot necesita Gestionar servidor para ver qué invitación usó cada miembro.",
                      "Uitnodigingen: de bot heeft Server beheren nodig om te zien welke uitnodiging elk lid gebruikte.",
                      "Einladungen: Der Bot braucht „Server verwalten“, um zu sehen, welche Einladung jedes Mitglied genutzt hat."),
    "check_reply_here": ("Replies here: the bot needs {missing} in this channel.", None, "Réponses ici : le bot a besoin de {missing} dans ce salon.",
                         "Respuestas aquí: el bot necesita {missing} en este canal.", "Antwoorden hier: de bot heeft {missing} nodig in dit kanaal.",
                         "Antworten hier: Der Bot braucht {missing} in diesem Kanal."),
    "check_unseen": ("Not recorded, because the bot can't see them: {count} channels.", None,
                     "Non enregistrés, car le bot ne les voit pas : {count} salons.", "No se registran porque el bot no los ve: {count} canales.",
                     "Niet vastgelegd, omdat de bot ze niet ziet: {count} kanalen.", "Nicht erfasst, weil der Bot sie nicht sieht: {count} Kanäle."),
    "check_tag_clash": ("Typing these now runs a statistics command, not the tag with that name: {names}. /tag get still runs the tag.", None,
                        "Taper ces noms lance maintenant une commande de statistiques, et non le tag du même nom : {names}. /tag get lance toujours le tag.",
                        "Escribir estos nombres ahora ejecuta un comando de estadísticas, no la etiqueta con ese nombre: {names}. /tag get sigue ejecutando la etiqueta.",
                        "Deze namen typen start nu een statistiekcommando, niet de tag met die naam: {names}. /tag get start de tag nog steeds.",
                        "Wer diese Namen tippt, startet jetzt einen Statistikbefehl statt des Tags mit diesem Namen: {names}. /tag get startet den Tag weiterhin."),
    "check_untracked": ("Left out on purpose by the server's setting: {count} channels or categories.", None,
                        "Exclus volontairement par le réglage du serveur : {count} salons ou catégories.",
                        "Excluidos a propósito por el ajuste del servidor: {count} canales o categorías.",
                        "Bewust weggelaten door de serverinstelling: {count} kanalen of categorieën.",
                        "Absichtlich durch die Servereinstellung ausgelassen: {count} Kanäle oder Kategorien."),
    "check_commands_here": ("Statistics commands can't be used in this channel: {why}", None, "Les commandes de statistiques ne marchent pas dans ce salon : {why}",
                            "Los comandos de estadísticas no se pueden usar en este canal: {why}", "Statistiekcommando's werken niet in dit kanaal: {why}",
                            "Statistikbefehle gehen in diesem Kanal nicht: {why}"),
    "check_threads": ("Recounts that include threads need Manage Threads.", None, "Les recomptages qui incluent les fils ont besoin de Gérer les fils.",
                      "Los recuentos con hilos necesitan Gestionar hilos.", "Hertellingen met threads hebben Threads beheren nodig.",
                      "Neuzählungen mit Threads brauchen „Threads verwalten“."),
    "check_roles": ("Stat roles need Manage Roles.", None, "Les rôles statistiques ont besoin de Gérer les rôles.", "Los roles por estadísticas necesitan Gestionar roles.",
                    "Statistiekrollen hebben Rollen beheren nodig.", "Statistikrollen brauchen „Rollen verwalten“."),
    "check_counters": ("Counter channels need Manage Channels and Connect.", None, "Les salons compteurs ont besoin de Gérer les salons et Se connecter.",
                       "Los canales contadores necesitan Gestionar canales y Conectar.", "Tellerkanalen hebben Kanalen beheren en Verbinden nodig.",
                       "Zählerkanäle brauchen „Kanäle verwalten“ und „Verbinden“."),
    "check_channels_file": ("The file lists every channel the bot can't read.", None, "Le fichier liste chaque salon que le bot ne peut pas lire.",
                            "El archivo enumera cada canal que el bot no puede leer.", "Het bestand somt elk kanaal op dat de bot niet kan lezen.",
                            "Die Datei listet jeden Kanal, den der Bot nicht lesen kann."),
    "check_recording": ("Recording", None, "Enregistrement", "Registro", "Vastleggen", "Erfassung"),
    "check_problems": ("Needs a look", None, "À vérifier", "Requiere atención", "Even nakijken", "Prüfen"),
    "check_fine": ("Working", None, "Fonctionne", "Funciona", "Werkt", "Funktioniert"),
    "check_messages_ok": ("Messages and voice: recorded in {count} channels.", None, "Messages et vocal : enregistrés dans {count} salons.",
                          "Mensajes y voz: se registran en {count} canales.", "Berichten en spraak: vastgelegd in {count} kanalen.",
                          "Nachrichten und Sprache: in {count} Kanälen erfasst."),
}

# Each unit's (singular, plural) per language, in the order of LANGUAGES. None in the UK column means the same as US.
UNITS: dict[str, tuple] = {
    "message": (("message", "messages"), None, ("message", "messages"), ("mensaje", "mensajes"), ("bericht", "berichten"), ("Nachricht", "Nachrichten")),
    "member": (("member", "members"), None, ("membre", "membres"), ("miembro", "miembros"), ("lid", "leden"), ("Mitglied", "Mitglieder")),
    "minutes": (("minute", "minutes"), None, ("minute", "minutes"), ("minuto", "minutos"), ("minuut", "minuten"), ("Minute", "Minuten")),
    "hours": (("hour", "hours"), None, ("heure", "heures"), ("hora", "horas"), ("uur", "uur"), ("Stunde", "Stunden")),
    "days": (("day", "days"), None, ("jour", "jours"), ("día", "días"), ("dag", "dagen"), ("Tag", "Tage")),
    "use": (("use", "uses"), None, ("utilisation", "utilisations"), ("uso", "usos"), ("keer", "keer"), ("Nutzung", "Nutzungen")),
    "join": (("join", "joins"), None, ("arrivée", "arrivées"), ("entrada", "entradas"), ("toetreding", "toetredingen"), ("Beitritt", "Beitritte")),
    "leave": (("leave", "leaves"), None, ("départ", "départs"), ("salida", "salidas"), ("vertrek", "vertrekken"), ("Austritt", "Austritte")),
    "channel": (("channel", "channels"), None, ("salon", "salons"), ("canal", "canales"), ("kanaal", "kanalen"), ("Kanal", "Kanäle")),
}
UNIT_SHORT = {  # what fits after a number in a narrow column
    "minutes": ("min", None, "min", "min", "min", "Min."),
    "hours": ("h", None, "h", "h", "u", "Std."),
    "days": ("d", None, "j", "d", "d", "T"),
}

_MONTHS = {
    "en-US": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "fr": ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."),
    "es-ES": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic"),
    "nl": ("jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"),
    "de": ("Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."),
}
_MONTHS["en-GB"] = _MONTHS["en-US"]
_GROUP = {"en-US": ",", "en-GB": ",", "fr": " ", "es-ES": ".", "nl": ".", "de": "."}  # French groups with a space
_DECIMAL = {"en-US": ".", "en-GB": ".", "fr": ",", "es-ES": ",", "nl": ",", "de": ","}
# CLDR's short forms: (thousands, millions, billions). None leaves the number whole, as German does below a million.
_COMPACT = {
    "en-US": ("{}K", "{}M", "{}B"), "en-GB": ("{}K", "{}M", "{}B"), "fr": ("{} k", "{} M", "{} Md"),
    "es-ES": ("{} mil", "{} M", "{} mil M"), "nl": ("{}K", "{} mln.", "{} mld."), "de": (None, "{} Mio.", "{} Mrd."),
}


def t(lang: str, key: str, **values: Any) -> str:
    """One string in a language, with its {placeholders} filled in."""
    row = TEXT[key]
    index = _INDEX.get(lang, 0)
    text = row[index] if row[index] is not None else row[0]
    return text.format(**values) if values else text


def unit(lang: str, key: str, value: float, shown: str | None = None) -> str:
    """A unit's word for this many, singular or plural by the language's rule. `shown` is the number as printed, which decides English's
    "1 hour" against "1.0 hours"."""
    row = UNITS[key]
    index = _INDEX.get(lang, 0)
    one, other = row[index] if row[index] is not None else row[0]
    if lang == "fr":
        return one if 0 <= abs(value) < 2 else other
    printed = shown if shown is not None else number(lang, value)
    return one if printed == "1" else other


def short_unit(lang: str, key: str) -> str:
    row = UNIT_SHORT[key]
    index = _INDEX.get(lang, 0)
    return row[index] if row[index] is not None else row[0]


def number(lang: str, value: float, decimals: int = 0) -> str:
    """A number grouped the language's way: 12,345.6 in English, 12 345,6 in French, 12.345,6 in German. Spanish leaves four-digit numbers whole."""
    negative = value < 0
    rounded = Decimal(str(abs(value))).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)  # 12.5 reads as 13, as people round
    text = f"{rounded:,.{decimals}f}"
    whole, _, fraction = text.partition(".")
    if lang == "es-ES" and len(whole.replace(",", "")) <= 4:
        whole = whole.replace(",", "")
    whole = whole.replace(",", _GROUP.get(lang, ","))
    out = whole + (_DECIMAL.get(lang, ".") + fraction if fraction else "")
    return ("-" if negative else "") + out


def compact(lang: str, value: float) -> str:
    """A number short enough for a tile, to three significant digits: 2.37K, 29.2K, 125K, 1.2M. Below 1,000 it's whole."""
    value = float(value)
    magnitude = abs(value)
    forms = _COMPACT.get(lang, _COMPACT["en-US"])
    level = 2 if magnitude >= 1e9 else 1 if magnitude >= 1e6 else 0 if magnitude >= 1e3 else None
    if level is not None and forms[level] is not None:
        scaled = value / 10 ** (3 * (level + 1))
        decimals = 2 if abs(scaled) < 10 else 1 if abs(scaled) < 100 else 0
        if round(abs(scaled), decimals) >= 1000 and level < 2 and forms[level + 1] is not None:  # 999,999 reads as 1M, not 1000K
            level += 1
            scaled = value / 10 ** (3 * (level + 1))
            decimals = 2
        return forms[level].format(trim(number(lang, scaled, decimals), lang))
    if magnitude < 1000 and value != int(value):
        return trim(number(lang, value, 2 if magnitude < 10 else 1), lang)
    return number(lang, value)


def trim(text: str, lang: str) -> str:
    """12.50 reads as 12.5, and 3.00 as 3."""
    mark = _DECIMAL.get(lang, ".")
    if mark not in text:
        return text
    text = text.rstrip("0")
    return text[:-1] if text.endswith(mark) else text


def figure(lang: str, value: float) -> str:
    """A count as a card prints it: whole below 100,000, short above."""
    return number(lang, value) if abs(value) < 100_000 else compact(lang, value)


def amount(lang: str, value: float, key: str) -> str:
    shown = figure(lang, value)
    return f"{shown} {unit(lang, key, value, shown)}"


def voice_value(seconds: float, time_unit: str) -> float:
    return seconds / {"minutes": 60, "hours": 3600, "days": 86400}.get(time_unit, 3600)


def voice_figure(lang: str, seconds: float, time_unit: str) -> str:
    """Voice time in the server's unit, as a card prints it: up to two decimals, fewer as it grows."""
    value = voice_value(seconds, time_unit)
    if value >= 100_000:
        return compact(lang, value)
    decimals = 0 if time_unit == "minutes" or value >= 1000 else 1 if value >= 100 else 2
    return trim(number(lang, value, decimals), lang)


def voice_amount(lang: str, seconds: float, time_unit: str) -> str:
    shown = voice_figure(lang, seconds, time_unit)
    return f"{shown} {unit(lang, time_unit, voice_value(seconds, time_unit), shown)}"


def percent(lang: str, share: float) -> str:
    """A share from 0 to 1 as a percentage, in the language's style: 12.5% in English, 12,5 % in French and German."""
    value = share * 100
    text = trim(number(lang, value, 1 if value < 10 else 0), lang)
    return f"{text} %" if lang in ("fr", "de", "es-ES") else f"{text}%"


def _local(timestamp: float, tz: Any) -> dt.datetime:
    return dt.datetime.fromtimestamp(timestamp, tz)


def date(lang: str, timestamp: float, tz: Any) -> str:
    """A day the way the language writes it in a short form: Nov 8, 2026 / 8 Nov 2026 / 8 nov. 2026 / 8 nov 2026 / 08.11.2026."""
    local = _local(timestamp, tz)
    return day_text(lang, local.date())


def day_text(lang: str, day: dt.date) -> str:
    month = _MONTHS.get(lang, _MONTHS["en-US"])[day.month - 1]
    if lang == "en-US":
        return f"{month} {day.day}, {day.year}"
    if lang == "de":
        return f"{day.day:02d}.{day.month:02d}.{day.year}"
    return f"{day.day} {month} {day.year}"


def day_month(lang: str, day: dt.date) -> str:
    """A day without its year, for a chart's axis."""
    month = _MONTHS.get(lang, _MONTHS["en-US"])[day.month - 1]
    if lang == "en-US":
        return f"{month} {day.day}"
    if lang == "de":
        return f"{day.day}. {month}"
    return f"{day.day} {month}"


def hour_text(lang: str, hour: int) -> str:
    if lang == "en-US":
        return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"
    if lang == "fr":
        return f"{hour} h"
    if lang == "de":
        return f"{hour} Uhr"
    return f"{hour:02d}:00"


def bucket(lang: str, start: float, interval: str, tz: Any) -> str:
    """A chart bucket's label: the hour, the day, the week's first day or the month."""
    local = _local(start, tz)
    if interval == "hour":
        return f"{day_month(lang, local.date())}, {hour_text(lang, local.hour)}"
    if interval in ("day", "week"):
        return day_month(lang, local.date())
    month = _MONTHS.get(lang, _MONTHS["en-US"])[local.month - 1]
    return f"{month} {local.year}"


def period_label(lang: str, lookback: dict, tz: Any = None) -> str:
    """A period as people read it: Last 14 days, All time, Since Mar 1, 2025."""
    mode = lookback.get("mode")
    if mode == "all":
        return t(lang, "all_time")
    if mode == "since" and lookback.get("since"):
        return t(lang, "since", date=day_text(lang, dt.date.fromisoformat(lookback["since"])))
    days = int(lookback.get("days") or 14)
    return t(lang, "last_day") if days == 1 else t(lang, "last_days", days=number(lang, days))


def days_label(lang: str, days: int) -> str:
    """A card's row label: 1 day, 7 days, 30 days."""
    return t(lang, "day_1") if days == 1 else t(lang, "days_n", days=number(lang, days))


def joined(lang: str, items: list[str], limit: int = 5) -> str:
    """Up to `limit` items in a comma list, and how many more."""
    if len(items) <= limit:
        return ", ".join(items)
    return t(lang, "and_more", items=", ".join(items[:limit]), count=number(lang, len(items) - limit))
