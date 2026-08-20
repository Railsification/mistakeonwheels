# cogs/rebus.py
from __future__ import annotations

__version__ = "1.0.0"

import asyncio
import random
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.game_stats import record_solo_result, register_game
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.storage import load_guild_json, save_guild_json
from core.utils import ensure_deferred

GAMES_FILENAME = "rebus_games.json"
CYCLE_FILENAME = "rebus_puzzle_cycle.json"
SKIP_VOTES_REQUIRED = 3
FUZZY_MATCH_CUTOFF = 0.86
MAX_GUESSES_SHOWN = 10

# Puzzle text is deliberately plain-text/monospace so it renders consistently on
# mobile Discord without needing image files or a media-vault setup.
DEFAULT_PUZZLES: tuple[dict[str, Any], ...] = (
    {
        "id": "man_overboard",
        "puzzle": "    MAN\nBOARD",
        "answer": "Man overboard",
        "aliases": ["man over board"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Look at where MAN is compared with BOARD.", "It is also an emergency call at sea."],
    },
    {
        "id": "head_over_heels",
        "puzzle": "HEAD\n\nHEELS",
        "answer": "Head over heels",
        "aliases": [],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["One body part is above another.", "It can describe being deeply in love."],
    },
    {
        "id": "mind_over_matter",
        "puzzle": "MIND\n\nMATTER",
        "answer": "Mind over matter",
        "aliases": [],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Read the vertical relationship.", "Mental strength defeating physical difficulty."],
    },
    {
        "id": "i_understand",
        "puzzle": "STAND\n  I",
        "answer": "I understand",
        "aliases": ["i under stand"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Where is I located?", "Say the layout out loud."],
    },
    {
        "id": "tricycle",
        "puzzle": "CYCLE  CYCLE  CYCLE",
        "answer": "Tricycle",
        "aliases": ["tri cycle", "three cycles"],
        "category": "Repetition",
        "difficulty": "Easy",
        "hints": ["Count the repeated word.", "The prefix means three."],
    },
    {
        "id": "split_decision",
        "puzzle": "DECI     SION",
        "answer": "Split decision",
        "aliases": ["a split decision"],
        "category": "Spacing",
        "difficulty": "Easy",
        "hints": ["The word itself has been separated.", "A close result can be decided this way."],
    },
    {
        "id": "backward_glance",
        "puzzle": "ECNALG",
        "answer": "Backward glance",
        "aliases": ["a backward glance", "backwards glance"],
        "category": "Direction",
        "difficulty": "Easy",
        "hints": ["Read the letters in the opposite direction.", "A quick look behind you."],
    },
    {
        "id": "broken_promise",
        "puzzle": "PROM\nISE",
        "answer": "Broken promise",
        "aliases": ["a broken promise"],
        "category": "Split word",
        "difficulty": "Easy",
        "hints": ["The word has been broken into pieces.", "Something you failed to keep."],
    },
    {
        "id": "between_jobs",
        "puzzle": "JOB   IN   JOB",
        "answer": "In between jobs",
        "aliases": ["between jobs"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["IN is located between two identical words.", "A polite way to say unemployed."],
    },
    {
        "id": "between_you_and_me",
        "puzzle": "YOU   JUST   ME",
        "answer": "Just between you and me",
        "aliases": ["between you and me", "just between us"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Which word sits between YOU and ME?", "Usually said before sharing a secret."],
    },
    {
        "id": "reading_between_lines",
        "puzzle": "----------------\n     READING\n----------------",
        "answer": "Reading between the lines",
        "aliases": ["read between the lines"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["READING is literally located somewhere.", "It means finding a hidden meaning."],
    },
    {
        "id": "degrees_below_zero",
        "puzzle": "        0\n   M.D.   Ph.D.",
        "answer": "Two degrees below zero",
        "aliases": ["2 degrees below zero", "two degrees under zero"],
        "category": "Position",
        "difficulty": "Medium",
        "hints": ["M.D. and Ph.D. are both types of something.", "Count them and note their position under 0."],
    },
    {
        "id": "downtown",
        "puzzle": "DOWN\nTOWN",
        "answer": "Downtown",
        "aliases": ["down town"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Read from top to bottom.", "A city-centre district."],
    },
    {
        "id": "good_afternoon",
        "puzzle": "NOON   GOOD",
        "answer": "Good afternoon",
        "aliases": ["good after noon"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["GOOD appears after NOON.", "A daytime greeting."],
    },
    {
        "id": "life_after_death",
        "puzzle": "DEATH   LIFE",
        "answer": "Life after death",
        "aliases": [],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Which word comes after DEATH?", "A common spiritual concept."],
    },
    {
        "id": "long_time_no_see",
        "puzzle": "LOOOOOONG   TIME",
        "answer": "Long time no see",
        "aliases": ["long time no c"],
        "category": "Wordplay",
        "difficulty": "Medium",
        "hints": ["TIME follows a very long word.", "There is also no letter C anywhere."],
    },
    {
        "id": "middle_age",
        "puzzle": "AG  MIDDLE  E",
        "answer": "Middle age",
        "aliases": ["middle aged"],
        "category": "Inside",
        "difficulty": "Easy",
        "hints": ["MIDDLE has been placed inside AGE.", "A stage of adulthood."],
    },
    {
        "id": "top_secret",
        "puzzle": "SECRET\n\n\n\n",
        "answer": "Top secret",
        "aliases": ["top-secret"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["The word is placed at the very top.", "A high security classification."],
    },
    {
        "id": "bottom_line",
        "puzzle": "\n\n\n\nLINE",
        "answer": "Bottom line",
        "aliases": ["the bottom line"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["The word is placed at the very bottom.", "It can mean the final result or main point."],
    },
    {
        "id": "crossroads",
        "puzzle": "   R\n   O\nROAD\n   D",
        "answer": "Crossroads",
        "aliases": ["cross roads", "cross road"],
        "category": "Shape",
        "difficulty": "Medium",
        "hints": ["The same word travels in two directions.", "A place where routes meet."],
    },
    {
        "id": "once_in_lifetime",
        "puzzle": "L1IFETIME",
        "answer": "Once in a lifetime",
        "aliases": ["one in a lifetime", "1 in a lifetime"],
        "category": "Inside",
        "difficulty": "Medium",
        "hints": ["The number 1 is inside LIFETIME.", "Something exceptionally rare."],
    },
    {
        "id": "one_in_million",
        "puzzle": "MILL1ION",
        "answer": "One in a million",
        "aliases": ["1 in a million"],
        "category": "Inside",
        "difficulty": "Easy",
        "hints": ["The number 1 is inside MILLION.", "Someone exceptionally special."],
    },
    {
        "id": "half_baked",
        "puzzle": "BAK",
        "answer": "Half baked",
        "aliases": ["half-baked"],
        "category": "Missing letters",
        "difficulty": "Medium",
        "hints": ["Only half of BAKED is present.", "An idea that was not properly thought through."],
    },
    {
        "id": "missing_link",
        "puzzle": "L  NK",
        "answer": "Missing link",
        "aliases": ["the missing link"],
        "category": "Missing letters",
        "difficulty": "Easy",
        "hints": ["One letter is absent from LINK.", "A connection needed to complete a chain."],
    },
    {
        "id": "stand_in_line",
        "puzzle": "L  STAND  INE",
        "answer": "Stand in line",
        "aliases": ["standing in line"],
        "category": "Inside",
        "difficulty": "Medium",
        "hints": ["STAND has been inserted into LINE.", "Something done while waiting your turn."],
    },
    {
        "id": "upside_down",
        "puzzle": "PU",
        "answer": "Upside down",
        "aliases": ["up side down"],
        "category": "Direction",
        "difficulty": "Easy",
        "hints": ["UP has been reversed.", "The opposite of right-way up."],
    },
    {
        "id": "leftovers",
        "puzzle": "LEFT\nOVERS",
        "answer": "Leftovers",
        "aliases": ["left overs"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["LEFT is over OVERS.", "Food saved after a meal."],
    },
    {
        "id": "over_and_over_again",
        "puzzle": "OVER  OVER  OVER  AGAIN",
        "answer": "Over and over again",
        "aliases": ["over over again"],
        "category": "Repetition",
        "difficulty": "Easy",
        "hints": ["OVER repeats before AGAIN.", "It means repeatedly."],
    },
    {
        "id": "repeat_after_me",
        "puzzle": "ME   REPEAT",
        "answer": "Repeat after me",
        "aliases": [],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["REPEAT appears after ME.", "A teacher may say this before a phrase."],
    },
    {
        "id": "under_arrest",
        "puzzle": "ARREST\n  U R",
        "answer": "You are under arrest",
        "aliases": ["u r under arrest"],
        "category": "Position",
        "difficulty": "Medium",
        "hints": ["Read U R aloud and note its position.", "A police officer may say it."],
    },
    {
        "id": "growing_economy",
        "puzzle": "E\nEC\nECO\nECON\nECONO\nECONOM\nECONOMY",
        "answer": "Growing economy",
        "aliases": ["a growing economy"],
        "category": "Pattern",
        "difficulty": "Medium",
        "hints": ["The word gains one letter each line.", "A positive financial trend."],
    },
    {
        "id": "falling_asleep",
        "puzzle": "SLEEP\n  SLEEP\n    SLEEP\n      SLEEP",
        "answer": "Falling asleep",
        "aliases": ["falling to sleep"],
        "category": "Direction",
        "difficulty": "Easy",
        "hints": ["SLEEP moves downward each time.", "What happens when you become unconscious at night."],
    },
    {
        "id": "double_cross",
        "puzzle": "CROSS   CROSS",
        "answer": "Double cross",
        "aliases": ["double-cross"],
        "category": "Repetition",
        "difficulty": "Easy",
        "hints": ["Count the word CROSS.", "It can also mean betrayal."],
    },
    {
        "id": "multiple_choice",
        "puzzle": "CHOICE  CHOICE  CHOICE  CHOICE",
        "answer": "Multiple choice",
        "aliases": ["multiple choices"],
        "category": "Repetition",
        "difficulty": "Easy",
        "hints": ["There is more than one CHOICE.", "A common exam question format."],
    },
    {
        "id": "back_to_square_one",
        "puzzle": "+-------+\n|   1   |\n+-------+",
        "answer": "Back to square one",
        "aliases": ["square one", "back at square one"],
        "category": "Shape",
        "difficulty": "Medium",
        "hints": ["The number 1 is enclosed by a shape.", "It means starting again from the beginning."],
    },
    {
        "id": "square_meal",
        "puzzle": "+-------+\n| MEAL  |\n+-------+",
        "answer": "Square meal",
        "aliases": ["a square meal"],
        "category": "Shape",
        "difficulty": "Easy",
        "hints": ["MEAL is inside a square.", "A proper, filling meal."],
    },
    {
        "id": "black_sheep",
        "puzzle": "SHEEP  SHEEP  SHEEP\nSHEEP  BLACK  SHEEP\nSHEEP  SHEEP  SHEEP",
        "answer": "Black sheep",
        "aliases": ["the black sheep", "black sheep of the family"],
        "category": "Odd one out",
        "difficulty": "Easy",
        "hints": ["One item is different from all the surrounding sheep.", "The odd or disfavoured member of a group."],
    },
    {
        "id": "three_blind_mice",
        "puzzle": "M CE   M CE   M CE",
        "answer": "Three blind mice",
        "aliases": ["3 blind mice"],
        "category": "Missing letters",
        "difficulty": "Medium",
        "hints": ["Count the incomplete word MICE.", "Each one is missing an eye — the letter I."],
    },
    {
        "id": "four_corners",
        "puzzle": "4             4\n\n\n4             4",
        "answer": "Four corners",
        "aliases": ["4 corners"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Count the numbers and look where they sit.", "A rectangle has this many."],
    },
    {
        "id": "stand_up_comedy",
        "puzzle": "COMEDY\nSTAND",
        "answer": "Stand-up comedy",
        "aliases": ["stand up comedy"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["COMEDY is above STAND.", "A live performance by a comedian."],
    },
    {
        "id": "breakfast",
        "puzzle": "BREAK\nFAST",
        "answer": "Breakfast",
        "aliases": ["break fast"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Combine the two stacked words.", "Usually the first meal of the day."],
    },
    {
        "id": "high_five",
        "puzzle": "5\n\n\n\n",
        "answer": "High five",
        "aliases": ["high 5"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["The number is placed very high.", "A celebratory hand gesture."],
    },
    {
        "id": "low_profile",
        "puzzle": "\n\n\nPROFILE",
        "answer": "Low profile",
        "aliases": ["a low profile"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["PROFILE is placed very low.", "Keeping one means avoiding attention."],
    },
    {
        "id": "split_level",
        "puzzle": "LE     VEL",
        "answer": "Split level",
        "aliases": ["split-level"],
        "category": "Split word",
        "difficulty": "Easy",
        "hints": ["LEVEL has been divided.", "A building design with floors at different heights."],
    },
    {
        "id": "second_hand",
        "puzzle": "HAND   HAND",
        "answer": "Second hand",
        "aliases": ["secondhand"],
        "category": "Repetition",
        "difficulty": "Easy",
        "hints": ["Which HAND is the answer referring to?", "It can mean pre-owned."],
    },
    {
        "id": "under_cover",
        "puzzle": "UNDER\nCOVER",
        "answer": "Undercover",
        "aliases": ["under cover"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["COVER is under UNDER.", "A concealed identity or investigation."],
    },
    {
        "id": "water_under_bridge",
        "puzzle": "BRIDGE\n~~~~ WATER ~~~~",
        "answer": "Water under the bridge",
        "aliases": ["water under bridge"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["WATER is below BRIDGE.", "Past trouble that no longer matters."],
    },
    {
        "id": "broken_heart",
        "puzzle": "HE     ART",
        "answer": "Broken heart",
        "aliases": ["a broken heart", "heart broken"],
        "category": "Split word",
        "difficulty": "Easy",
        "hints": ["HEART has been separated.", "Emotional pain after loss or rejection."],
    },
    {
        "id": "heart_of_gold",
        "puzzle": "HE  GOLD  ART",
        "answer": "Heart of gold",
        "aliases": ["a heart of gold"],
        "category": "Inside",
        "difficulty": "Medium",
        "hints": ["GOLD is inside HEART.", "A phrase for someone extremely kind."],
    },
    {
        "id": "turn_back_time",
        "puzzle": "EMIT",
        "answer": "Turn back time",
        "aliases": ["time turned back", "backwards time"],
        "category": "Direction",
        "difficulty": "Easy",
        "hints": ["Read the letters backwards.", "A wish to return to the past."],
    },
    {
        "id": "time_after_time",
        "puzzle": "TIME   TIME",
        "answer": "Time after time",
        "aliases": ["time and time again"],
        "category": "Repetition",
        "difficulty": "Easy",
        "hints": ["One TIME comes after another.", "It means repeatedly."],
    },
    {
        "id": "inside_out",
        "puzzle": "I  OUT  N",
        "answer": "Inside out",
        "aliases": ["inside-out"],
        "category": "Inside",
        "difficulty": "Medium",
        "hints": ["OUT is inside the letters of IN.", "Clothing can accidentally be worn this way."],
    },
    {
        "id": "box_office",
        "puzzle": "+----------+\n|  OFFICE  |\n+----------+",
        "answer": "Box office",
        "aliases": ["the box office"],
        "category": "Shape",
        "difficulty": "Easy",
        "hints": ["OFFICE is enclosed by a box.", "Where tickets are sold, or a film's earnings."],
    },
    {
        "id": "out_of_order",
        "puzzle": "O  D  R  E  R",
        "answer": "Out of order",
        "aliases": ["order out of order"],
        "category": "Scrambled",
        "difficulty": "Easy",
        "hints": ["The letters of ORDER are not in their proper sequence.", "A sign commonly placed on broken equipment."],
    },
    {
        "id": "man_in_moon",
        "puzzle": "MO  MAN  ON",
        "answer": "Man in the moon",
        "aliases": ["the man in the moon"],
        "category": "Inside",
        "difficulty": "Medium",
        "hints": ["MAN is inside MOON.", "A familiar imagined lunar face."],
    },
    {
        "id": "once_upon_time",
        "puzzle": "ONCE\nTIME",
        "answer": "Once upon a time",
        "aliases": ["once on a time"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["ONCE is sitting upon TIME.", "The classic beginning of a fairy tale."],
    },
    {
        "id": "eye_for_eye",
        "puzzle": "EYE   4   EYE",
        "answer": "An eye for an eye",
        "aliases": ["eye for an eye", "eye 4 eye"],
        "category": "Number wordplay",
        "difficulty": "Easy",
        "hints": ["Read the number 4 as a word.", "A phrase about equal retaliation."],
    },
    {
        "id": "foregone_conclusion",
        "puzzle": "4   GONE   CONCLUSION",
        "answer": "Foregone conclusion",
        "aliases": ["a foregone conclusion", "four gone conclusion"],
        "category": "Number wordplay",
        "difficulty": "Medium",
        "hints": ["Read 4 and GONE together aloud.", "An outcome regarded as certain."],
    },
    {
        "id": "four_wheel_drive",
        "puzzle": "WHEEL  WHEEL  WHEEL  WHEEL",
        "answer": "Four-wheel drive",
        "aliases": ["four wheel drive", "4 wheel drive", "four wheels"],
        "category": "Repetition",
        "difficulty": "Easy",
        "hints": ["Count the WHEELs.", "A vehicle drivetrain often shortened to 4WD."],
    },
    {
        "id": "all_for_one",
        "puzzle": "ALL  4  1\n1  4  ALL",
        "answer": "All for one and one for all",
        "aliases": ["all 4 one and one 4 all"],
        "category": "Number wordplay",
        "difficulty": "Medium",
        "hints": ["Read 4 as FOR and 1 as ONE.", "The famous Three Musketeers motto."],
    },
    {
        "id": "lucky_break",
        "puzzle": "LUC     KY",
        "answer": "Lucky break",
        "aliases": ["a lucky break"],
        "category": "Split word",
        "difficulty": "Easy",
        "hints": ["LUCKY has been broken apart.", "An unexpected piece of good fortune."],
    },
    {
        "id": "double_bed",
        "puzzle": "BED   BED",
        "answer": "Double bed",
        "aliases": ["a double bed"],
        "category": "Repetition",
        "difficulty": "Easy",
        "hints": ["Count the word BED.", "A bed intended for two people."],
    },
    {
        "id": "bed_of_roses",
        "puzzle": "BED\nROSES ROSES ROSES",
        "answer": "Bed of roses",
        "aliases": ["a bed of roses"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["The BED sits on ROSES.", "A situation that is easy or pleasant."],
    },
    {
        "id": "rose_between_thorns",
        "puzzle": "THORN   ROSE   THORN",
        "answer": "A rose between two thorns",
        "aliases": ["rose between two thorns", "rose between thorns"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Look at what surrounds ROSE.", "Count the THORNs."],
    },
    {
        "id": "two_left_feet",
        "puzzle": "FEET  FEET                         ",
        "answer": "Two left feet",
        "aliases": ["2 left feet"],
        "category": "Position",
        "difficulty": "Medium",
        "hints": ["There are two FEET and both are placed to the left.", "It describes someone who is clumsy at dancing."],
    },
    {
        "id": "right_between_eyes",
        "puzzle": "EYE   RIGHT   EYE",
        "answer": "Right between the eyes",
        "aliases": ["right between your eyes"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["RIGHT is positioned between two EYEs.", "It can describe a direct impact."],
    },
    {
        "id": "all_eyes_on_me",
        "puzzle": "EYE   EYE\n   ME\nEYE   EYE",
        "answer": "All eyes on me",
        "aliases": ["all eyes are on me"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Every EYE surrounds the same word.", "It means being the centre of attention."],
    },
    {
        "id": "seeing_double",
        "puzzle": "SEEING   SEEING",
        "answer": "Seeing double",
        "aliases": ["see double"],
        "category": "Repetition",
        "difficulty": "Easy",
        "hints": ["SEEING appears twice.", "It can happen when your vision is impaired."],
    },
    {
        "id": "word_for_word",
        "puzzle": "WORD   4   WORD",
        "answer": "Word for word",
        "aliases": ["word 4 word"],
        "category": "Number wordplay",
        "difficulty": "Easy",
        "hints": ["Read 4 aloud.", "It means exactly as originally spoken or written."],
    },
    {
        "id": "foot_in_mouth",
        "puzzle": "MO  FOOT  UTH",
        "answer": "Foot in mouth",
        "aliases": ["put your foot in your mouth", "foot in your mouth"],
        "category": "Inside",
        "difficulty": "Medium",
        "hints": ["FOOT has been put inside MOUTH.", "It means saying something embarrassing or tactless."],
    },
    {
        "id": "backseat_driver",
        "puzzle": "SEAT   DRIVER",
        "answer": "Backseat driver",
        "aliases": ["back seat driver"],
        "category": "Position",
        "difficulty": "Medium",
        "hints": ["DRIVER appears behind SEAT when read left to right.", "A passenger who gives unwanted driving advice."],
    },
    {
        "id": "first_in_line",
        "puzzle": "1----------------",
        "answer": "First in line",
        "aliases": ["first on line", "1st in line"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["The number 1 begins the line.", "The person at the front of a queue."],
    },
    {
        "id": "end_of_line",
        "puzzle": "----------------END",
        "answer": "End of the line",
        "aliases": ["the end of the line", "end of line"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["END is placed at the end of a line.", "It can mean there are no further options."],
    },
    {
        "id": "split_second",
        "puzzle": "SEC     OND",
        "answer": "Split second",
        "aliases": ["a split second"],
        "category": "Split word",
        "difficulty": "Easy",
        "hints": ["SECOND has been divided.", "An extremely brief moment."],
    },
    {
        "id": "second_to_none",
        "puzzle": "SECOND   2   NONE",
        "answer": "Second to none",
        "aliases": ["second 2 none"],
        "category": "Number wordplay",
        "difficulty": "Easy",
        "hints": ["Read 2 as TO.", "It means the very best."],
    },
    {
        "id": "last_man_standing",
        "puzzle": "FALLEN  FALLEN  FALLEN\n          MAN",
        "answer": "Last man standing",
        "aliases": ["the last man standing"],
        "category": "Odd one out",
        "difficulty": "Medium",
        "hints": ["Everyone else has FALLEN.", "One MAN remains."],
    },
    {
        "id": "down_to_earth",
        "puzzle": "DOWN\n  TO\nEARTH",
        "answer": "Down to earth",
        "aliases": ["down-to-earth"],
        "category": "Position",
        "difficulty": "Easy",
        "hints": ["Read the stacked words from top to bottom.", "It describes someone practical and unpretentious."],
    },
    {
        "id": "worlds_apart",
        "puzzle": "WORLD                         WORLD",
        "answer": "Worlds apart",
        "aliases": ["world apart"],
        "category": "Spacing",
        "difficulty": "Easy",
        "hints": ["The two WORLDs are separated by a large distance.", "It means extremely different."],
    },
    {
        "id": "middle_of_nowhere",
        "puzzle": "NO   WORLD   WHERE",
        "answer": "In the middle of nowhere",
        "aliases": ["middle of nowhere", "world in the middle of nowhere"],
        "category": "Inside",
        "difficulty": "Medium",
        "hints": ["WORLD is between NO and WHERE.", "A very remote place."],
    },
    {
        "id": "back_door",
        "puzzle": "ROOD",
        "answer": "Back door",
        "aliases": ["backdoor", "backwards door"],
        "category": "Direction",
        "difficulty": "Easy",
        "hints": ["DOOR has been written backwards.", "An unofficial or hidden way in."],
    },
    {
        "id": "door_to_door",
        "puzzle": "DOOR   2   DOOR",
        "answer": "Door to door",
        "aliases": ["door 2 door"],
        "category": "Number wordplay",
        "difficulty": "Easy",
        "hints": ["Read 2 as TO.", "A sales or delivery method."],
    },
    {
        "id": "step_by_step",
        "puzzle": "STEP\n  STEP\n    STEP",
        "answer": "Step by step",
        "aliases": ["step-by-step"],
        "category": "Pattern",
        "difficulty": "Easy",
        "hints": ["Each STEP follows another like stairs.", "It means progressing one stage at a time."],
    },
    {
        "id": "room_for_one_more",
        "puzzle": "ROOM   4   1   MORE",
        "answer": "Room for one more",
        "aliases": ["room 4 one more", "room for 1 more"],
        "category": "Number wordplay",
        "difficulty": "Easy",
        "hints": ["Read 4 as FOR and 1 as ONE.", "A question about whether another person can fit."],
    },
    {
        "id": "elbow_room",
        "puzzle": "RO  ELBOW  OM",
        "answer": "Elbow room",
        "aliases": ["elbowroom"],
        "category": "Inside",
        "difficulty": "Medium",
        "hints": ["ELBOW is inside ROOM.", "Enough space to move comfortably."],
    },
    {
        "id": "room_and_board",
        "puzzle": "ROOM   &   BOARD",
        "answer": "Room and board",
        "aliases": ["room & board"],
        "category": "Symbol wordplay",
        "difficulty": "Easy",
        "hints": ["Read the ampersand as AND.", "Accommodation plus meals."],
    },
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_NUMBER_EQUIVALENTS: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
    "first": "1",
    "second": "2",
    "third": "3",
    "fourth": "4",
    "fifth": "5",
    "sixth": "6",
    "seventh": "7",
    "eighth": "8",
    "ninth": "9",
    "tenth": "10",
}


def _normalise_number_token(token: str) -> str:
    mapped = _NUMBER_EQUIVALENTS.get(token)
    if mapped is not None:
        return mapped

    ordinal = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", token)
    if ordinal:
        return ordinal.group(1)

    return token


def _normalise_guess(value: str) -> str:
    text = str(value or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [
        _normalise_number_token(token)
        for token in text.split()
        if token not in {"a", "an", "the"}
    ]
    return " ".join(tokens)


def _token_overlap_ratio(left: str, right: str) -> float:
    left_tokens = left.split()
    right_tokens = right.split()
    if not left_tokens or not right_tokens:
        return 0.0

    remaining = list(right_tokens)
    matches = 0
    for token in left_tokens:
        if token in remaining:
            matches += 1
            remaining.remove(token)

    return matches / max(len(left_tokens), len(right_tokens))


def _safe_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    output: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number not in output:
            output.append(number)
    return output


def _safe_guess_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        display = re.sub(r"\s+", " ", str(item or "")).strip()
        normalised = _normalise_guess(display)
        if not display or not normalised or normalised in seen:
            continue
        seen.add(normalised)
        output.append(display[:100])
    return output


def _format_guess(value: str) -> str:
    # Keep user-entered guesses readable without allowing them to break the
    # inline-code formatting used in the puzzle embed.
    return str(value or "").replace("`", "'").strip()


def _validate_puzzles() -> None:
    seen: set[str] = set()
    for puzzle in DEFAULT_PUZZLES:
        puzzle_id = str(puzzle.get("id") or "").strip()
        answer = str(puzzle.get("answer") or "").strip()
        display = str(puzzle.get("puzzle") or "").strip("\n")
        if not puzzle_id or puzzle_id in seen:
            raise RuntimeError(f"Invalid or duplicate Rebus puzzle ID: {puzzle_id!r}")
        if not answer or not display:
            raise RuntimeError(f"Rebus puzzle {puzzle_id!r} is missing its display or answer")
        seen.add(puzzle_id)


_validate_puzzles()


class GuessRebusModal(discord.ui.Modal, title="Solve the rebus"):
    answer = discord.ui.TextInput(
        label="Your answer",
        placeholder="Type the phrase or saying",
        min_length=1,
        max_length=100,
        required=True,
    )

    def __init__(
        self,
        service: "RebusService",
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ):
        super().__init__()
        self.service = service
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.message_id = int(message_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.submit_guess(
            interaction,
            str(self.answer.value),
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            message_id=self.message_id,
        )


class RebusView(discord.ui.View):
    def __init__(self, service: "RebusService"):
        super().__init__(timeout=None)
        self.service = service

    @discord.ui.button(
        label="Answer",
        emoji="🧠",
        style=discord.ButtonStyle.success,
        custom_id="hotbot:rebus:answer:v1",
    )
    async def answer_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not self.service.is_active_game_message(interaction):
            await interaction.response.send_message(
                "That is not the current Rebus puzzle in this channel.",
                ephemeral=True,
            )
            return

        if interaction.message is None or interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("That puzzle is no longer available.", ephemeral=True)
            return

        await interaction.response.send_modal(
            GuessRebusModal(
                self.service,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                message_id=interaction.message.id,
            )
        )

    @discord.ui.button(
        label="Hint",
        emoji="💡",
        style=discord.ButtonStyle.primary,
        custom_id="hotbot:rebus:hint:v1",
    )
    async def hint_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.service.reveal_hint(interaction)

    @discord.ui.button(
        label="Skip",
        emoji="⏭️",
        style=discord.ButtonStyle.secondary,
        custom_id="hotbot:rebus:skip:v1",
    )
    async def skip_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.service.vote_skip(interaction)


class RebusService:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    def _lock_for(self, guild_id: int, channel_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(channel_id))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def allowed(self, guild_id: int | None, channel_id: int | None) -> bool:
        if not guild_id or not channel_id:
            return False
        return self.settings.is_feature_allowed(guild_id, channel_id, "games")

    def _load_games_blob(self, guild_id: int) -> dict[str, Any]:
        raw = load_guild_json(guild_id, GAMES_FILENAME, {"games": {}})
        if not isinstance(raw, dict):
            raw = {"games": {}}
        if not isinstance(raw.get("games"), dict):
            raw["games"] = {}
        return raw

    def _save_games_blob(self, guild_id: int, blob: dict[str, Any]) -> None:
        save_guild_json(guild_id, GAMES_FILENAME, blob)

    def _get_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        blob = self._load_games_blob(guild_id)
        game = blob["games"].get(str(channel_id))
        return game if isinstance(game, dict) else None

    def _set_game(self, guild_id: int, channel_id: int, game: dict[str, Any]) -> None:
        blob = self._load_games_blob(guild_id)
        blob["games"][str(channel_id)] = game
        self._save_games_blob(guild_id, blob)

    def _remove_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        blob = self._load_games_blob(guild_id)
        old = blob["games"].pop(str(channel_id), None)
        self._save_games_blob(guild_id, blob)
        return old if isinstance(old, dict) else None

    def is_active_game_message(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild_id or not interaction.channel_id or interaction.message is None:
            return False
        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if not game:
            return False
        return int(game.get("message_id") or 0) == interaction.message.id

    def start_issue(self, interaction: discord.Interaction) -> str | None:
        if interaction.guild is None or interaction.channel_id is None:
            return "This game must be started in a server channel."
        if not self.allowed(interaction.guild_id, interaction.channel_id):
            return "❌ Rebus can only be used in the configured games channel(s)."

        existing = self._get_game(interaction.guild.id, interaction.channel_id)
        if not existing:
            return None

        message_id = int(existing.get("message_id") or 0)
        jump = (
            f"https://discord.com/channels/{interaction.guild.id}/{interaction.channel_id}/{message_id}"
            if message_id
            else ""
        )
        text = "A Rebus puzzle is already open in this channel."
        if jump:
            text += f" [Open it]({jump})"
        return text

    def _pick_puzzle(self, guild_id: int) -> dict[str, Any]:
        puzzle_by_id = {str(puzzle["id"]): puzzle for puzzle in DEFAULT_PUZZLES}
        valid_ids = set(puzzle_by_id)
        state = load_guild_json(
            guild_id,
            CYCLE_FILENAME,
            {"used_ids": [], "last_id": ""},
        )
        if not isinstance(state, dict):
            state = {"used_ids": [], "last_id": ""}

        used_ids = {
            str(item)
            for item in state.get("used_ids", [])
            if str(item) in valid_ids
        }
        last_id = str(state.get("last_id") or "")
        available = [puzzle_id for puzzle_id in valid_ids if puzzle_id not in used_ids]

        if not available:
            used_ids.clear()
            available = [
                puzzle_id
                for puzzle_id in valid_ids
                if len(valid_ids) == 1 or puzzle_id != last_id
            ]
            if not available:
                available = list(valid_ids)

        selected_id = random.choice(sorted(available))
        used_ids.add(selected_id)
        save_guild_json(
            guild_id,
            CYCLE_FILENAME,
            {"used_ids": sorted(used_ids), "last_id": selected_id},
        )
        return puzzle_by_id[selected_id]

    def _accepted_answers(self, game: dict[str, Any]) -> list[str]:
        raw_values = [game.get("answer"), *(game.get("aliases") or [])]
        accepted: list[str] = []
        for raw in raw_values:
            normalised = _normalise_guess(str(raw or ""))
            if normalised and normalised not in accepted:
                accepted.append(normalised)
        return accepted

    def _is_correct_guess(self, game: dict[str, Any], raw_guess: str) -> bool:
        guess = _normalise_guess(raw_guess)
        if not guess:
            return False

        accepted = self._accepted_answers(game)
        if guess in accepted:
            return True

        # Number words and digits are normalised before this point, so answers such
        # as "Back to square one" and "Back to square 1" are equivalent. Fuzzy
        # matching then allows a small typo or one harmless missing word without
        # accepting a merely related phrase.
        if len(guess.replace(" ", "")) < 6:
            return False

        for answer in accepted:
            ratio = SequenceMatcher(None, guess, answer).ratio()
            overlap = _token_overlap_ratio(guess, answer)

            if ratio >= 0.93:
                return True

            if ratio >= FUZZY_MATCH_CUTOFF and overlap >= 0.60:
                return True

        return False

    def _can_control(self, interaction: discord.Interaction, game: dict[str, Any]) -> bool:
        if interaction.user.id == int(game.get("started_by") or 0):
            return True
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        permissions = member.guild_permissions
        return permissions.administrator or permissions.manage_messages

    def _record_solve(self, guild_id: int, game: dict[str, Any], user_id: int) -> None:
        message_id = int(game.get("message_id") or 0)
        created_at = str(game.get("created_at") or "unknown")
        result_key = str(message_id) if message_id else created_at
        try:
            record_solo_result(
                guild_id,
                "rebus",
                user_id,
                event_id=f"rebus:{guild_id}:{result_key}",
                label="Rebus Puzzles",
                result_word="solve",
            )
        except Exception as exc:
            warn(f"rebus stats update failed: {exc!r}")

    def _build_embed(
        self,
        game: dict[str, Any],
        *,
        status: str = "active",
        ended_by: int | None = None,
    ) -> discord.Embed:
        puzzle_text = str(game.get("puzzle") or "Puzzle unavailable")
        answer = str(game.get("answer") or "Unknown answer")
        winner_id = int(game.get("winner_id") or 0)

        if status == "won":
            embed = discord.Embed(
                title="🧩 Rebus Puzzle — Solved!",
                description=(
                    f"🎉 <@{winner_id}> solved it!\n\n"
                    f"```\n{puzzle_text}\n```\n"
                    f"**Answer:** {answer}"
                ),
                colour=discord.Colour.green(),
            )
        elif status == "skipped":
            embed = discord.Embed(
                title="⏭️ Rebus Puzzle — Skipped",
                description=f"```\n{puzzle_text}\n```\n**Answer:** {answer}",
                colour=discord.Colour.orange(),
            )
        elif status == "ended":
            who = f" by <@{ended_by}>" if ended_by else ""
            embed = discord.Embed(
                title="🛑 Rebus Puzzle — Ended",
                description=(
                    f"Puzzle ended{who}.\n\n"
                    f"```\n{puzzle_text}\n```\n"
                    f"**Answer:** {answer}"
                ),
                colour=discord.Colour.dark_grey(),
            )
        else:
            embed = discord.Embed(
                title="🧩 Rebus Puzzle",
                description=f"What phrase or saying is shown below?\n```\n{puzzle_text}\n```",
                colour=discord.Colour.blurple(),
            )

        embed.add_field(
            name="Category",
            value=str(game.get("category") or "General"),
            inline=True,
        )
        embed.add_field(
            name="Difficulty",
            value=str(game.get("difficulty") or "Unknown"),
            inline=True,
        )
        embed.add_field(
            name="Started by",
            value=f"<@{int(game.get('started_by') or 0)}>",
            inline=True,
        )

        if status == "active":
            hints = [str(item) for item in game.get("hints", []) if str(item).strip()]
            hint_count = max(0, min(len(hints), int(game.get("hint_count") or 0)))
            shown_hints = hints[:hint_count]
            embed.add_field(
                name=f"Hints ({hint_count}/{len(hints)})",
                value=(
                    "\n".join(f"**{index}.** {hint}" for index, hint in enumerate(shown_hints, start=1))
                    if shown_hints
                    else "No hints revealed yet."
                ),
                inline=False,
            )
            guesses = _safe_guess_list(game.get("guesses"))
            wrong_guesses = max(len(guesses), int(game.get("wrong_guesses") or 0))
            visible_guesses = guesses[-MAX_GUESSES_SHOWN:]
            hidden_count = max(0, len(guesses) - len(visible_guesses))

            if visible_guesses:
                guess_lines = [f"• `{_format_guess(item)}`" for item in visible_guesses]
                if hidden_count:
                    guess_lines.insert(0, f"*…and {hidden_count} earlier guess{'es' if hidden_count != 1 else ''}*.")
                guesses_value = "\n".join(guess_lines)
            elif wrong_guesses:
                guesses_value = "Earlier guesses were not saved by the previous Rebus version."
            else:
                guesses_value = "No guesses yet."

            embed.add_field(
                name=f"Guesses tried ({wrong_guesses})",
                value=guesses_value,
                inline=False,
            )

            skip_votes = len(_safe_int_list(game.get("skip_votes")))
            embed.add_field(
                name="Skip votes",
                value=f"{skip_votes} / {SKIP_VOTES_REQUIRED}",
                inline=True,
            )
            embed.set_footer(
                text="Anyone can answer or reveal hints. Starter/admin can skip immediately; otherwise 3 votes. No timeout."
            )
        else:
            embed.set_footer(text="Use /rebus or /games to start the next puzzle.")

        return embed

    async def _fetch_game_message(self, game: dict[str, Any]) -> discord.Message | None:
        channel_id = int(game.get("channel_id") or 0)
        message_id = int(game.get("message_id") or 0)
        if not channel_id or not message_id:
            return None
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            return await channel.fetch_message(message_id)  # type: ignore[attr-defined]
        except Exception as exc:
            warn(f"rebus fetch message failed: {exc!r}")
            return None

    async def _edit_game_message(
        self,
        game: dict[str, Any],
        *,
        status: str = "active",
        ended_by: int | None = None,
    ) -> None:
        message = await self._fetch_game_message(game)
        if message is None:
            return
        view: discord.ui.View | None = RebusView(self) if status == "active" else None
        try:
            await message.edit(
                embed=self._build_embed(game, status=status, ended_by=ended_by),
                view=view,
            )
        except Exception as exc:
            warn(f"rebus edit message failed: {exc!r}")

    async def start_game(self, interaction: discord.Interaction) -> None:
        issue = self.start_issue(interaction)
        if issue:
            await interaction.followup.send(issue, ephemeral=True)
            return

        guild = interaction.guild
        channel_id = interaction.channel_id
        if guild is None or channel_id is None:
            await interaction.followup.send(
                "This game must be started in a server channel.",
                ephemeral=True,
            )
            return

        async with self._lock_for(guild.id, channel_id):
            existing = self._get_game(guild.id, channel_id)
            if existing:
                message_id = int(existing.get("message_id") or 0)
                jump = (
                    f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"
                    if message_id
                    else ""
                )
                text = "A Rebus puzzle is already open in this channel."
                if jump:
                    text += f" [Open it]({jump})"
                await interaction.followup.send(text, ephemeral=True)
                return

            puzzle = self._pick_puzzle(guild.id)
            game: dict[str, Any] = {
                "puzzle_id": str(puzzle["id"]),
                "puzzle": str(puzzle["puzzle"]),
                "answer": str(puzzle["answer"]),
                "aliases": list(puzzle.get("aliases") or []),
                "category": str(puzzle.get("category") or "General"),
                "difficulty": str(puzzle.get("difficulty") or "Unknown"),
                "hints": list(puzzle.get("hints") or []),
                "hint_count": 0,
                "wrong_guesses": 0,
                "guesses": [],
                "skip_votes": [],
                "started_by": interaction.user.id,
                "guild_id": guild.id,
                "channel_id": channel_id,
                "message_id": 0,
                "created_at": _utc_now(),
            }

            message = await interaction.followup.send(
                embed=self._build_embed(game),
                view=RebusView(self),
                ephemeral=False,
                wait=True,
            )
            game["message_id"] = int(message.id)
            self._set_game(guild.id, channel_id, game)

    async def submit_guess(
        self,
        interaction: discord.Interaction,
        raw_guess: str,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        guess = _normalise_guess(raw_guess)
        if not guess:
            await interaction.response.send_message("Enter a phrase or saying.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != int(message_id):
                await interaction.followup.send(
                    "That Rebus puzzle is no longer active.",
                    ephemeral=True,
                )
                return

            if self._is_correct_guess(game, raw_guess):
                game["winner_id"] = interaction.user.id
                self._record_solve(guild_id, game, interaction.user.id)
                self._remove_game(guild_id, channel_id)
                await self._edit_game_message(game, status="won")
                await interaction.followup.send("🎉 Correct — you solved it!", ephemeral=True)
                return

            guesses = _safe_guess_list(game.get("guesses"))
            if any(_normalise_guess(item) == guess for item in guesses):
                await interaction.followup.send(
                    "That answer has already been tried.",
                    ephemeral=True,
                )
                return

            display_guess = re.sub(r"\s+", " ", str(raw_guess or "")).strip()[:100]
            guesses.append(display_guess)
            game["guesses"] = guesses
            game["wrong_guesses"] = max(0, int(game.get("wrong_guesses") or 0)) + 1
            self._set_game(guild_id, channel_id, game)
            await self._edit_game_message(game)
            await interaction.followup.send("Not quite — try again.", ephemeral=True)

    async def reveal_hint(self, interaction: discord.Interaction) -> None:
        if not self.is_active_game_message(interaction):
            await interaction.response.send_message(
                "That is not the current Rebus puzzle in this channel.",
                ephemeral=True,
            )
            return

        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("This only works in a server channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game:
                await interaction.followup.send("There is no active Rebus puzzle here.", ephemeral=True)
                return

            hints = [str(item) for item in game.get("hints", []) if str(item).strip()]
            current = max(0, int(game.get("hint_count") or 0))
            if current >= len(hints):
                await interaction.followup.send("All available hints are already showing.", ephemeral=True)
                return

            game["hint_count"] = current + 1
            self._set_game(guild_id, channel_id, game)
            await self._edit_game_message(game)
            await interaction.followup.send(
                f"💡 Hint {current + 1} revealed for everyone.",
                ephemeral=True,
            )

    async def vote_skip(self, interaction: discord.Interaction) -> None:
        if not self.is_active_game_message(interaction):
            await interaction.response.send_message(
                "That is not the current Rebus puzzle in this channel.",
                ephemeral=True,
            )
            return

        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("This only works in a server channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game:
                await interaction.followup.send("There is no active Rebus puzzle here.", ephemeral=True)
                return

            if self._can_control(interaction, game):
                self._remove_game(guild_id, channel_id)
                await self._edit_game_message(game, status="skipped")
                await interaction.followup.send("⏭️ Puzzle skipped.", ephemeral=True)
                return

            votes = _safe_int_list(game.get("skip_votes"))
            if interaction.user.id in votes:
                await interaction.followup.send("You have already voted to skip.", ephemeral=True)
                return

            votes.append(interaction.user.id)
            game["skip_votes"] = votes
            if len(votes) >= SKIP_VOTES_REQUIRED:
                self._remove_game(guild_id, channel_id)
                await self._edit_game_message(game, status="skipped")
                await interaction.followup.send(
                    "⏭️ Skip vote passed. The answer has been revealed.",
                    ephemeral=True,
                )
                return

            self._set_game(guild_id, channel_id, game)
            await self._edit_game_message(game)
            await interaction.followup.send(
                f"Skip vote added: **{len(votes)} / {SKIP_VOTES_REQUIRED}**.",
                ephemeral=True,
            )

    async def end_game(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.followup.send("This only works in the game channel.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game:
                await interaction.followup.send("There is no active Rebus puzzle here.", ephemeral=True)
                return

            if not self._can_control(interaction, game):
                await interaction.followup.send(
                    "Only the person who started the puzzle or a server moderator can end it. Use the Skip button to vote.",
                    ephemeral=True,
                )
                return

            self._remove_game(guild_id, channel_id)
            await self._edit_game_message(game, status="ended", ended_by=interaction.user.id)
            await interaction.followup.send("Rebus puzzle ended.", ephemeral=True)


class RebusCog(commands.Cog):
    GAME_META = {
        "key": "rebus",
        "label": "Rebus Puzzles",
        "kind": "solo",
        "result_word": "solve",
        "description": "Solve visual word-and-phrase puzzles with hints and skip voting",
        "emoji": "🧩",
        "requires_opponent": False,
    }

    HELP_META = {
        "title": "Rebus Puzzles",
        "summary": "A persistent channel-wide rebus game with hints, fuzzy answers, skip voting and leaderboard solves.",
        "goal": "Work out the phrase or saying represented by the displayed words, spacing, numbers or layout.",
        "how_to_play": (
            "Start with `/rebus` or choose Rebus Puzzles from `/games`. Press **Answer** to "
            "submit a private guess; attempted answers are then shown on the puzzle so the "
            "channel can see what has already been tried. Press **Hint** to reveal the next "
            "hint publicly. The first accepted answer solves the puzzle."
        ),
        "rules": (
            "Reasonable spelling variations, number-word equivalents and small typos can be "
            "accepted by the fuzzy-answer matcher. The person who started the puzzle or a "
            "moderator can skip/end it immediately. Other players use **Skip** and need "
            "**3 skip votes**. Revealed hints, guesses and skip votes persist through normal "
            "Railway restarts."
        ),
        "details": (
            "Use `/rebus` or choose Rebus Puzzles from `/games`. Anyone can answer or "
            "reveal hints. The starter or a moderator can end/skip immediately; everyone "
            "else can vote to skip."
        ),
    }

    def __init__(self, bot: commands.Bot, service: RebusService):
        self.bot = bot
        self.service = service
        # Register immediately so Rebus appears in dynamic leaderboard menus even
        # before the server records its first solve.
        register_game(
            "rebus",
            label="Rebus Puzzles",
            kind="solo",
            result_word="solve",
        )

    @app_commands.command(name="rebus", description="Start a Rebus puzzle in this channel")
    async def rebus(self, interaction: discord.Interaction) -> None:
        log_cmd("rebus", interaction)
        if not await ensure_deferred(interaction, ephemeral=False):
            return
        await self.service.start_game(interaction)

    @app_commands.command(
        name="rebus_end",
        description="End the current Rebus puzzle (starter or moderator)",
    )
    async def rebus_end(self, interaction: discord.Interaction) -> None:
        log_cmd("rebus_end", interaction)
        if not await ensure_deferred(interaction, ephemeral=True):
            return
        await self.service.end_game(interaction)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    service = RebusService(bot)

    # Persistent custom IDs keep active puzzle buttons working after Railway
    # restarts and normal GitHub deployments.
    bot.add_view(RebusView(service))

    cog = RebusCog(bot, service)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
