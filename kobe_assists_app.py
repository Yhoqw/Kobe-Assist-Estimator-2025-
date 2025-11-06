
"""Kobe Assist Calculator GUI

This module provides a small Tkinter GUI to calculate a custom "Kobe Assist"
metric for NBA players by analyzing play-by-play data from the `nba_api`.

High-level flow:
- Load player list for a chosen season
- Sample the most recent N games for a selected player
- For each missed shot by the player, inspect the following plays to
    determine if an offensive rebound and subsequent score occurred
    (within a short window) — these points are counted as 'Kobe assists'.

The module focuses on clarity rather than performance and inserts
small sleeps to respect the NBA API rate limits.
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
from datetime import datetime
import pandas as pd
from nba_api.stats.endpoints import leaguedashplayerstats, playbyplayv2, playergamelog
import time

# --- NBA API Data Functions ---

def get_all_players(season):
    """Return a DataFrame with basic player info for the requested season.

    The returned DataFrame contains (at minimum) the columns:
    - PLAYER_NAME
    - PLAYER_ID
    - GP (games played)

    On any error (network, API change, etc.) an empty DataFrame is returned
    so callers can handle the failure without an exception.

    Args:
        season (str): NBA season string accepted by nba_api (e.g. "2023-24").

    Returns:
        pd.DataFrame: player info (possibly empty on error).
    """
    try:
        player_stats = leaguedashplayerstats.LeagueDashPlayerStats(season=season).get_data_frames()[0]
        return player_stats[['PLAYER_NAME', 'PLAYER_ID', 'GP']].sort_values(by='PLAYER_NAME').reset_index(drop=True)
    except Exception:
        # Return empty dataframe on any error to keep GUI responsive
        return pd.DataFrame()

def get_sample_games(player_id, season, n=15):
    """Return a list of recent `n` Game_IDs for a player in the given season.

    The function sleeps briefly to help avoid hitting NBA API rate limits.
    If anything goes wrong it returns an empty list.

    Args:
        player_id (int): NBA player id
        season (str): season string used by nba_api
        n (int): maximum number of recent games to return

    Returns:
        list[int]: list of Game_ID values (may be empty).
    """
    # Small sleep to respect public API rate limits
    time.sleep(0.6)
    try:
        game_log = playergamelog.PlayerGameLog(player_id=player_id, season=season).get_data_frames()[0]
        sample_games = game_log.head(n)
        return sample_games['Game_ID'].tolist()
    except Exception:
        return []

# --- Kobe Assist Calculation Logic ---

def is_missed_shot(play):
    """Return True if the play record represents a missed field goal.

    The `EVENTMSGTYPE` field is used by the NBA play-by-play feed where
    type 2 indicates a missed shot.
    """
    return play.get('EVENTMSGTYPE', 0) == 2

def is_offensive_rebound(play, shooting_team_id):
    """Return True when a play is an offensive rebound for the shooting team.

    The NBA feed uses `EVENTMSGTYPE == 4` for rebounds. We compare
    the rebounding player's team id to the provided shooting_team_id
    to determine whether it is an offensive rebound.
    """
    event_type = play.get('EVENTMSGTYPE', 0)
    team_id = play.get('PLAYER1_TEAM_ID', 0)
    return event_type == 4 and team_id == shooting_team_id

def is_score(play):
    """Return True if a play is a scoring event.

    In the play-by-play feed, `EVENTMSGTYPE` values 1 and 3 correspond
    to made field goals and made free throws respectively (or similar
    scoring events). This small helper centralizes that logic.
    """
    return play.get('EVENTMSGTYPE', 0) in [1, 3]

def extract_points_from_play(play):
    """Infer the number of points scored from a play record.

    The NBA play-by-play contains textual descriptions in
    `HOMEDESCRIPTION` and `VISITORDESCRIPTION`. We look for keywords
    to distinguish between 3PT, free throws, and regular 2-point
    field goals. This is a heuristic but sufficient for this metric.
    """
    description = str(play.get('HOMEDESCRIPTION', '')) + ' ' + str(play.get('VISITORDESCRIPTION', ''))
    if '3PT' in description or 'Three Point' in description:
        return 3
    elif 'Free Throw' in description:
        return 1
    else:
        return 2

def analyze_game_for_kobe_assists(game_id, player_name):
    """Analyze a single game's play-by-play and count Kobe Assist points.

    Steps:
    - Fetch play-by-play for the game_id.
    - Iterate plays and find missed shots by the `player_name`.
    - For each missed shot, call `check_kobe_assist_sequence` to inspect
      the subsequent plays for an offensive rebound and scoring sequence.

    Returns the total Kobe Assist points found in the game (int).
    """
    # Small sleep to avoid hammering the API in quick loops
    time.sleep(0.6)
    try:
        pbp = playbyplayv2.PlayByPlayV2(game_id=game_id).get_data_frames()[0]
        plays = pbp.to_dict('records')
        total_kobe_points = 0
        for i, play in enumerate(plays):
            if is_missed_shot(play) and play.get('PLAYER1_NAME', '') == player_name:
                shooting_team_id = play.get('PLAYER1_TEAM_ID', 0)
                points = check_kobe_assist_sequence(plays, i, shooting_team_id)
                total_kobe_points += points
        return total_kobe_points
    except Exception:
        # Return zero for games we couldn't analyze to keep the GUI running
        return 0

def check_kobe_assist_sequence(plays, missed_shot_index, shooting_team_id):
    """Inspect the plays immediately after a missed shot for a Kobe Assist.

    Rules implemented:
    - Look ahead up to a small window (7 plays) after the missed shot.
    - If an offensive rebound by the shooting team occurs, mark it found.
    - After an offensive rebound, if a scoring play by the same team occurs
      before a turnover or defensive rebound by the opposing team, the
      scored points are counted as Kobe Assist points.

    Args:
        plays (list[dict]): sequence of play dictionaries
        missed_shot_index (int): index of the missed shot in `plays`
        shooting_team_id (int): the team id that attempted the shot

    Returns:
        int: points attributed to the Kobe Assist from this sequence
    """
    total_points = 0
    found_offensive_rebound = False
    # Look ahead a small number of plays to keep the heuristic tight
    for i in range(missed_shot_index + 1, min(len(plays), missed_shot_index + 8)):
        current_play = plays[i]
        if not found_offensive_rebound and is_offensive_rebound(current_play, shooting_team_id):
            found_offensive_rebound = True

        if found_offensive_rebound:
            # If the team that got the offensive rebound scored, add points
            if is_score(current_play) and current_play.get('PLAYER1_TEAM_ID', 0) == shooting_team_id:
                total_points += extract_points_from_play(current_play)

            event_type = current_play.get('EVENTMSGTYPE', 0)
            if event_type == 5:  # Turnover - sequence broken
                break
            elif event_type == 4:  # Rebound event
                # Defensive rebound by the other team ends the sequence
                if current_play.get('PLAYER1_TEAM_ID', 0) != shooting_team_id:
                    break
    return total_points

def calculate_player_kobe_assist_average(app_instance, player_name, player_id, season, sample_size):
    """Background worker that computes the Kobe Assist average for a player.

    This function is intended to run in a separate thread so it may call
    GUI methods on `app_instance` to report progress (via `app_instance.log`).

    Args:
        app_instance (KobeAssistApp): GUI instance used for logging/progress
        player_name (str): full player name to match in play-by-play
        player_id (int): NBA player id used to fetch game logs
        season (str): season string
        sample_size (int): number of recent games to sample
    """
    app_instance.log(f"Starting calculation for {player_name}...")
    game_ids = get_sample_games(player_id, season, sample_size)
    if not game_ids:
        app_instance.log(f"Error: Could not find recent games for {player_name} in the {season} season.")
        return

    total_points = 0
    for i, game_id in enumerate(game_ids):
        points = analyze_game_for_kobe_assists(game_id, player_name)
        total_points += points
        # Report progress back to the GUI
        app_instance.log(f"  Game {i+1}/{len(game_ids)}: Found {points} Kobe assist points.")

    average = total_points / len(game_ids) if game_ids else 0.0
    app_instance.log(f"\nCalculation complete for {player_name}.")
    app_instance.log(f"Average Kobe Assists per game: {average:.2f}")


# --- GUI Application ---

class KobeAssistApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Kobe Assist Calculator")
        self.geometry("500x550")

        self.players_df = None
        self.player_names = []

        self.create_widgets()
        self.preload_seasons()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.calculation_thread = None

    def create_widgets(self):
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Season selection
        ttk.Label(main_frame, text="Select Season:").pack(fill=tk.X)
        self.season_var = tk.StringVar()
        self.season_combo = ttk.Combobox(main_frame, textvariable=self.season_var, state="readonly")
        self.season_combo.pack(fill=tk.X, pady=5)
        self.season_combo.bind("<<ComboboxSelected>>", self.on_season_change)

        # Player selection
        ttk.Label(main_frame, text="Select Player:").pack(fill=tk.X)
        self.player_var = tk.StringVar()
        self.player_combo = ttk.Combobox(main_frame, textvariable=self.player_var, state="disabled")
        self.player_combo.pack(fill=tk.X, pady=5)
        self.player_combo.bind("<<ComboboxSelected>>", self.on_player_change)
        self.player_combo.bind('<KeyRelease>', self.filter_players)

        # Sample size slider
        self.sample_size_frame = ttk.Frame(main_frame)
        self.sample_size_frame.pack(fill=tk.X, pady=5)
        self.sample_size_label = ttk.Label(self.sample_size_frame, text="Games to Sample (Max: 0):")
        self.sample_size_label.pack(side=tk.LEFT)
        self.sample_size_var = tk.IntVar(value=10)
        self.sample_size_slider = ttk.Scale(self.sample_size_frame, from_=1, to=82, orient=tk.HORIZONTAL, variable=self.sample_size_var, command=self.update_slider_label, state="disabled")
        self.sample_size_slider.pack(side=tk.RIGHT, fill=tk.X, expand=True)

        # Calculate button
        self.calc_button = ttk.Button(main_frame, text="Calculate", command=self.start_calculation, state="disabled")
        self.calc_button.pack(fill=tk.X, pady=10)

        # Log display
        ttk.Label(main_frame, text="Log:").pack(fill=tk.X)
        self.log_area = scrolledtext.ScrolledText(main_frame, height=15, state="disabled")
        self.log_area.pack(fill=tk.BOTH, expand=True)

    def preload_seasons(self):
        current_year = datetime.now().year
        self.season_combo['values'] = [f"{year}-{str(year+1)[-2:]}" for year in range(current_year, 2000, -1)]
        self.season_combo.set(f"{current_year-1}-{str(current_year)[-2:]}") # Default to last season
        self.on_season_change()
        
    def on_season_change(self, event=None):
        self.log("Loading player data for the selected season...")
        self.player_combo.set('')
        self.player_combo['state'] = 'disabled'
        self.calc_button['state'] = 'disabled'
        season = self.season_var.get()
        threading.Thread(target=self.load_players, args=(season,), daemon=True).start()

    def load_players(self, season):
        self.players_df = get_all_players(season)
        if not self.players_df.empty:
            self.player_names = self.players_df['PLAYER_NAME'].tolist()
            self.player_combo['values'] = self.player_names
            self.player_combo['state'] = 'normal'
            self.log(f"Player data for {season} loaded.")
        else:
            self.log(f"Could not load player data for {season}.")
            self.player_combo['state'] = 'disabled'

    def on_player_change(self, event=None):
        player_name = self.player_var.get()
        player_info = self.players_df[self.players_df['PLAYER_NAME'] == player_name]
        if not player_info.empty:
            games_played = player_info.iloc[0]['GP']
            self.sample_size_label.config(text=f"Games to Sample (Max: {games_played}):")
            self.sample_size_slider.config(to=games_played, state="normal")
            if self.sample_size_var.get() > games_played:
                self.sample_size_var.set(games_played)
            self.calc_button['state'] = 'normal'
        else:
            self.calc_button['state'] = 'disabled'
            self.sample_size_slider['state'] = 'disabled'

    def filter_players(self, event=None):
        search_term = self.player_var.get().lower()
        filtered_list = [name for name in self.player_names if search_term in name.lower()]
        self.player_combo['values'] = filtered_list

    def update_slider_label(self, value):
        self.sample_size_var.set(int(float(value)))
        # No need to update the label text here, it is static now
        pass

    def start_calculation(self):
        player_name = self.player_var.get()
        season = self.season_var.get()
        sample_size = self.sample_size_var.get()

        player_info = self.players_df[self.players_df['PLAYER_NAME'] == player_name]
        if player_info.empty:
            messagebox.showerror("Error", "Invalid player selected.")
            return
        player_id = player_info.iloc[0]['PLAYER_ID']

        self.calc_button['state'] = "disabled"
        self.calculation_thread = threading.Thread(target=calculate_player_kobe_assist_average, 
                                                 args=(self, player_name, player_id, season, sample_size), 
                                                 daemon=True)
        self.calculation_thread.start()
        self.check_thread()

    def check_thread(self):
        if self.calculation_thread.is_alive():
            self.after(100, self.check_thread)
        else:
            self.calc_button['state'] = "normal"

    def log(self, message):
        self.log_area.config(state="normal")
        self.log_area.insert(tk.END, message + "\n")
        self.log_area.config(state="disabled")
        self.log_area.see(tk.END)
    
    def on_closing(self):
        if self.calculation_thread and self.calculation_thread.is_alive():
            if messagebox.askokcancel("Quit", "A calculation is in progress. Are you sure you want to quit?"):
                self.destroy()
        else:
            self.destroy()

if __name__ == "__main__":
    app = KobeAssistApp()
    app.mainloop()
