import logging
import math
import requests
import time
import re

from bs4 import BeautifulSoup
import pandas as pd

import user

class MafiaBot:

    def __init__(self, game_url: str, game_master: str,
                 bot_userID:str, bot_password:str, loop_waittime_seconds:int,
                 post_push_interval:int):


        self.game_thread           = game_url
        self.thread_id             = int(game_url.split('-')[-1])
        self.game_master           = game_master
        self.bot_ID                = bot_userID
        self.bot_password          = bot_password
        self.post_push_interval    = post_push_interval

        self.current_day_start_post  = 1
        self.last_votecount_id       = 1

        self.player_list              = []

        # Load vote rights table
        self.vote_rights = pd.read_csv('vote_config.csv', sep=',')

        # use lowercase player names as keys, player column as true names
        self.vote_rights.index = self.vote_rights['player'].str.lower()

        # We'll use this table as the master table for real names
        self.real_names = self.vote_rights['player'].to_dict()
        self.real_names[self.game_master.lower()] = 'GM'

        self.vote_requests = pd.DataFrame({'player' : self.vote_rights['player'],
                                          'max_vote_requests': self.vote_rights['allowed_vote_requests'],
                                          'vote_requested':0})


        self.majority_reached         = False

        # Temporal fix until proper votecount request queue is implemented
        self.gm_vote_request          = False

        print('Mafia MV Bot started!')
        print('Game run by:', game_master)
        print('Bot ID is', self.bot_ID)

        logging.info(f'Bot started. Game run by {game_master}. Bot id is: {self.bot_ID}')

        self.run(loop_waittime_seconds)
    
    def run(self, update_tick:int):
        '''
        Main bot loop. Called in class constructor. It iterates each N seconds,
        as defined by the config file. For each iteration, it parses the game
        thread if we are on day phase, then counts all the votes and decides
        if a new vote count should be pushed.

        Parameters: 
        update_tick (int): Seconds to pass between bot iterations.

        Returns: None
        '''

        while(True):

            # Initialize empty vote table
            self.vote_table = pd.DataFrame(columns=['player', 'voted_by', 'post_id'])

            if self.is_day_phase(): # Daytime, count

                print('We are on day time!')

                self.last_votecount_id = self.get_last_votecount()

                if not self.majority_reached:

                    self.last_thread_post  = self.get_last_post()

                    logging.info(f'Starting vote count. Last vote count: {self.last_votecount_id}. Last reply: {self.last_thread_post}')

                    self._start_page = self.get_page_number_from_post(self.current_day_start_post)
                    self._page_count = self.request_page_count()

                    logging.info(f'Detected day start at page: {self._start_page}')
                    logging.info(f'Detected {self._page_count} pages')

                    for self._cur_page in range(self._start_page, (self._page_count + 1)):

                        logging.info(f'Checking page: {self._cur_page}')

                        self.get_votes_from_page(page_to_scan=self._cur_page)

                    logging.info('Finished counting.')
                    print('Finished counting')

                    if self.update_thread_vote_count():

                        logging.info('Pushing a new votecount')
                        self.push_vote_count()  
                    
                    else:
                        logging.info('Recent votecount detected. ')
                
                else:
                    logging.info('Majority already reached. Skipping...')
                    
            else:
                logging.info('Night phase detected. Skipping...')
                print('We are on night phase!')
            
            logging.info(f'Sleeping for {update_tick} seconds.')  

            print(f'Sleeping for {update_tick} seconds.')
            time.sleep(update_tick)
    


    #TODO: This method can and should be refactored
    def is_day_phase(self) -> bool:
        '''
        Parse the GM posts to figure out if the game is currently on game phase.

        Parameters: None

        Returns:
        True/False (bool): Whether game is on day phase.
        '''
        self._is_day_phase     = False

        self._gm_posts         = self.game_thread + '?u=' + self.game_master
        self._gm_posts         = requests.get(self._gm_posts).text

        # Get total gm pages
        self._gm_pages = self.get_page_count_from_page(self._gm_posts)

        # We'll start looping from the last page to the previous one
        for self._pagenum in range(self._gm_pages, 0, -1):

            self._request      = f'{self.game_thread}?u={self.game_master}&pagina={self._pagenum}'
            self._request      = requests.get(self._request).text
            self._current_page = BeautifulSoup(self._request, 'html.parser')

            #TODO: log these iterations?
            self._posts        = self._current_page.find_all('div', attrs={'data-num':True,
                                                                          'data-autor':True})
          
            for self._post in reversed(self._posts): # from more recent to older posts

                self._headers = self._post.find_all('h2') # get all GM h2 headers

                for self._pday in self._headers:
                    
                    self._phase_end     = re.findall('^Final del día [0-9]*', self._pday.text)
                    self._phase_start   = re.findall('^Día [0-9]*', self._pday.text)

                    if self._phase_end:
                      return self._is_day_phase
                      
                    elif self._phase_start: 

                        #TODO: We should start thinking about an standalone method here
                        if self.current_day_start_post < int(self._post['data-num']):
                            self.majority_reached = False
                            self.vote_requests['vote_requested'] = 0

                        self.current_day_start_post = int(self._post['data-num'])
                        self._is_day_phase = True
                        self.player_list = self.get_player_list(self.current_day_start_post)

        return self._is_day_phase


    def update_thread_vote_count(self) -> bool:
        '''
        Decides if a new vote count should be posted based on:
        
        a) Pending GM requests.\n
        b) How many messages were posted since the last vote count. This is used-defined.

        Parameters: None

        Returns:
        True/False (bool): Whether to push a new vote count.
        '''

        self._push = False

        self._posts_since_count = self.last_thread_post - self.last_votecount_id

        if (self._posts_since_count >= self.post_push_interval) or self.gm_vote_request:

            self._push           = True
            self.gm_vote_request = False
            
        return self._push


    def get_last_votecount(self) -> int:
        '''
        Parses the bot messages in the game thread to get the post id of the 
        last automated vote count pushed.
    
        Parameters: None

        Returns:
        An int representing the post id of the last automated vote count.
        '''

        self._bot_posts         = self.game_thread + '?u=' + self.bot_ID
        self._bot_posts         = requests.get(self._bot_posts).text

        self._last_votecount_id = 1 
        
        # Get total gm pages
        self._bot_pages = self.get_page_count_from_page(self._bot_posts)

         # We'll start looping from the last page to the previous one
        for self._pagenum in range(self._bot_pages, 0, -1):

            self._request      = f'{self.game_thread}?u={self.bot_ID}&pagina={self._pagenum}'
            self._request      = requests.get(self._request).text
            self._current_page = BeautifulSoup(self._request, 'html.parser')

            #TODO: log these iterations?
            self._posts        = self._current_page.find_all('div', attrs={'data-num':True,
                                                                          'data-autor':True})
          
            for self._post in reversed(self._posts): # from more recent to older posts

                self._headers = self._post.find_all('h2') #get all GM h2 headers

                for self._pcount in self._headers:
                    
                    self._last_count_id = re.findall('^Recuento de votos$', self._pcount.text)
                    self._last_count_was_lynch = re.findall('^Recuento de votos final$', self._pcount.text)

                    # Hacky way to cover oddball case of the bot 
                    # shutting down and a player editing after EoD
                    if self._last_count_was_lynch or self._last_count_id:

                        if self._last_count_was_lynch and self.current_day_start_post < int(self._post['data-num']):
                            self.majority_reached = True

                        self._last_votecount_id = int(self._post['data-num'])
                        return self._last_votecount_id
    
        
        return self._last_votecount_id

    
    def get_last_post(self) -> int:
        '''
        Parses the game thread messages to get the post id of the 
        last posted message.
    
        Parameters: None

        Returns:
        An int representing the post id of the last posted message.
        '''
        self._last_post_id = 1

        self._last_page = self.request_page_count()
        self._request   = f'{self.game_thread}/{self._last_page}'
        self._request   =  requests.get(self._request).text

        self._page      = BeautifulSoup(self._request, 'html.parser')

        self._all_posts = self._page.find_all('div', attrs={'data-num':True,
                                                            'data-autor':True})

        return int(self._all_posts[-1]['data-num'])


    def get_votes_from_page(self, page_to_scan:int):
        '''
        Parses a defined page of the game thread and retrieves all h4 
        HTML elements, which may be commands or votes. Each h4 element is evaluated
        to decide if a vote or a command was casted. 

        For a vote, the vote_player function will be called. For a command,
        the command routine is called instead.
    
        Parameters:\n
        page_to_scan (int): A game thread page to parse.

        Returns: None
        '''

        self._parsed_url = f'{self.game_thread}/{page_to_scan}'
        self._request    = requests.get(self._parsed_url).text
        
        self._parser = BeautifulSoup(self._request, 'html.parser')

        # MV has unqiue div elements for odd and even posts, 
        # and another one for the very first post of the page.
        # I'm ignoring edit div elements because players should not edit while
        # playing.

        self._posts = self._parser.findAll('div', class_ = ['cf post',
                                                'cf post z',
                                                'cf post first'
                                                ])

        for self._post in self._posts:

            self._author  = self._post['data-autor'].lower()
            self._post_id = int(self._post['data-num'])
            self._post_content = self._post.find('div', class_ = 'post-contents')
            self._post_commands = self._post_content.findAll('h4')

            if self._post_id > self.current_day_start_post:

                for self._command in self._post_commands:

                    self._victim = '' 
                    self._command = self._command.text.lower()
                    
                    if self._command == 'recuento':

                        self.vote_count_request(player=self._author,
                                                post_id=self._post_id)

                    elif self._command == 'desvoto':
                        self._victim = 'desvoto'
                        
                    elif self._command.startswith('voto'):

                        if self._command.endswith('no linchamiento'):
                            self._victim = 'no_lynch'

                        else:
                            self._victim = self._command.split(' ')[-1]

                       
                    
                    if self._victim != '': 
                        self.vote_player(player=self._author,
                                         victim=self._victim,
                                         post_id=self._post_id)



    def vote_count_request(self, player: str, post_id: int):
        '''
        This function is called after a possible vote count request by the game
        GM is parsed. It first evaluates if the request originated from the GM
        and then checks if the request post id is higher than the last automated
        vote count ID. 

        If the request ID is higher than  the last vote count id, the flag
        self.gm_vote_requests is set to True.

        Parameters:\n
        player (string): The author of the vote count request.
        post_id (int): The post ID of the vote request.

        Returns: None
        '''
        if player == self.game_master.lower():

           # GM request after our last vote count
           if post_id > self.last_votecount_id:
               self.gm_vote_request = True


    def request_page_count(self):
        '''
        Performs an HTML request of the game thread and parses the resulting
        HTML code to get the total page length of the thread.

        Parameters: None.

        Returns: 
        An int representing the total page length of the thread.
        '''
        
        self._request = requests.get(self.game_thread).text
        self._page_count = self.get_page_count_from_page(self._request)

        return self._page_count


    def get_page_count_from_page(self, request_text):
        '''
        Parses an used defined HTML code from mediavida.com to extract the page
        count of a given thread. To do so it uses BeautifulSoup as a parser engine.

        Parameters: 
        request_text: A string of HTML text from the requests.get library.

        Returns: 
        An int representing the total page length of the thread.
        '''

        # Let's try to parse the page count from the bottom  page panel
        self._page_count = 1
        try:
            self._panel_layout = BeautifulSoup(request_text, 'html.parser') 
            self._panel_layout = self._panel_layout.find('div', id = 'bottompanel')

            self._result = self._panel_layout.find_all('a')[-2].contents[0]
            self._page_count = int(self._result)
        except:
            self._page_count = 1
        
        return self._page_count


    def get_page_number_from_post(self, post_id:int):
        '''
        Calculate the page number of a given post by assuming each game thread
        page has 30 posts.

        Parameters: 
        post_id: The ID of the post of interest.

        Returns: 
        An int representing the page number of the input post.
        '''

        self._page_number = math.ceil(post_id / 30)

        return self._page_number 


    def get_player_list(self, start_day_post_id:int) -> list():
        '''
        Based on a given post ID representing the day start, this function parses
        said post to retrieve a list of alive players by extracting all <ol> and
        <a> elements. 

        This is based on a day start scheme which must be used for the bot to
        work properly.

        Parameters:\n 
        post_id: The ID of the post which starts the game day.

        Returns:\n 
        A list (collection) of (string) players.
        '''
        self._start_day_page = self.get_page_number_from_post(start_day_post_id)

        self._request_url = f'{self.game_thread}/{self._start_day_page}'
        self._request     = requests.get(self._request_url).text

        self._response  = BeautifulSoup(self._request, 'html.parser')
        self._all_posts = self._response.find_all('div', attrs={'data-num':True,
                                                                'data-autor':True})
        
        self._player_list = []

        for self._post in self._all_posts:

            # We found the post of interest
            if int(self._post['data-num']) == start_day_post_id: 

                # Get the first list. It should be the player lists according to the template
                self._players = self._post.find('ol').find_all('a')

                for self._player in self._players:
                    self._player_list.append(self._player.contents[0].lower())

                return self._player_list


    def is_valid_vote(self, player:str, victim:str) -> bool:
        '''
        Evaluates if a given vote is valid. A valid vote has to fulfill the following
        requirements:

        a) The victim can be voted: alive, playing and set as vote candidate in the vote rights table.\n
        b) The voting player must have casted less votes than their current limit.

        Parameters:\n 
        player (str): The player casting the vote.
        victim (str): The player who receives the vote.\n
        Returns:\n 
        True if the vote is valid, False otherwise.
        '''

        self._is_valid_vote = False

        if not self.majority_reached:

            if player in self.player_list or player == self.game_master.lower():

                if player == self.game_master.lower():
                    self._player_max_votes = 999

                else:
                    self._player_max_votes = self.vote_rights.loc[player, 'allowed_votes']

                self._player_current_votes = len(self.vote_table[self.vote_table['voted_by'] == player])
            
                if victim == 'desvoto' and self._player_current_votes > 0:
                    self._is_valid_vote = True
            
                elif victim  == 'no_lynch' and self._player_current_votes < self._player_max_votes:
                    self._is_valid_vote  = True
            
                elif victim in self.player_list:

                    if victim in self.vote_rights.index:

                        if self.vote_rights.loc[victim, 'can_be_voted'] == 1:

                            if self._player_current_votes < self._player_max_votes:

                                self._is_valid_vote = True
                    else:
                        logging.warning(f'Player {victim} is not on the vote rights table.')
        else:
            logging.info(f'Rejecting vote from {player} to {victim} because majority was already reached')
        
        return self._is_valid_vote

    
    def is_lynched(self, victim:str) -> bool:
        '''
        Checks if a given player has received enough votes to be lynched. This
        function evaluates if a given player accumulates enough votes by calculating
        the current absolute majority required and adding to it a player specific
        lynch modifier as defined in the vote rights table.

        Parameters:\n 
        victim (str): The player who receives the vote.\n
        Returns:\n 
        True if the player should be lynched.  False otherwise.
        '''
        self._lynched = False

        # Count this player votes
        self._lynch_votes = len(self.vote_table[self.vote_table['player'] == victim])
        self._player_majority = self.get_vote_majority() + self.vote_rights.loc[victim, 'mod_to_lynch']

        if self._lynch_votes >= self._player_majority:
            self._lynched = True
        
        return self._lynched

    
    def vote_player(self, player:str, victim:str, post_id:int):
        '''
        This function process votes and keeps track of the vote table. Votes are added or removed based on the victim. 

        Parameters:\n
        player (str): The player who casts the vote.\n
        victim (str): The player who receives the vote. Can be set to "desvoto" to remove a previously casted voted by player.\n
        post_id  (int): The post ID where the vote was casted.\n
        Returns:\n 
        True if the player should be lynched.  False otherwise.
        '''

        if self.is_valid_vote(player, victim):

            if victim == 'desvoto':
                self._old_vote = self.vote_table[self.vote_table['voted_by'] == player].index[0]
                self.vote_table.drop(self._old_vote, axis=0, inplace=True)

                logging.info(f'Player {player} unvoted at {post_id}')
            
            else:
               
                self.vote_table  = self.vote_table.append({'player': victim,
                                                           'voted_by': player,
                                                           'post_id': post_id},
                                                           ignore_index=True)
                
                logging.info(f'{player} voted {victim} at {post_id}')

                #Check if we have reached majority
                if self.is_lynched(victim):
                    self.lynch_player(victim, post_id)   

        else:
            logging.warning(f'Invalid vote by {player} at {post_id}. They voted {victim}')


    def lynch_player(self, victim:str, post_id:int):
        '''
        When this function is called, a new User object is built to push a vote
        count in which the lynch is  announced. It also sets self.majority_reached
        to True, indicating to the bot that no more votes are allowed until a new day starts. 

        Parameters:\n
        victim (str): The player to lynch.\n
        post_id  (int): The post ID with the vote that triggered the lynch.\n
        Returns: None
        '''

        self.majority_reached        = True


        self._user = user.User(thread_id=self.thread_id, 
                               thread_url= self.game_thread,
                               bot_id= self.bot_ID,
                               bot_password=self.bot_password,
                               game_master= self.game_master)
        
        self._user.push_lynch(last_votecount=self.translate_votecount_names(),
                              victim=self.real_names[victim],
                              post_id=post_id)

    
    def push_vote_count(self):
        '''
        When this function is called, a new User object is built to push a vote
        count using the current vote table. The object is deleted when the function
        ends.

        Parameters: None
        Returns: None
        '''
        
        self.User = user.User(thread_id= self.thread_id,
                              thread_url=self.game_thread,
                              bot_id=self.bot_ID,
                              bot_password=self.bot_password,
                              game_master=self.game_master)
                    
        self.User.push_votecount(vote_count=self.translate_votecount_names(),
                                 alive_players=len(self.player_list),
                                 vote_majority=self.get_vote_majority(),
                                 post_id=self.last_thread_post)

        del self.User


    def translate_votecount_names(self):
        '''
        This function translates lowercased player names to their actual mediavida
        names for a fancier vote count post. To do so, it relies on the vote_rights
        table, which features a player column. A dictionary is built in which
        the table index is the lowercased player name and the dict. values are taken
        from the player column. 

        The vote table names are then mapped using this dictionary.

        Parameters: None \n
        Returns: 
        A vote table pandas object in which lowercase names have been mapped to 
        their real mediavida names.
        '''

        self._translat_votetable = self.vote_table

        self._translat_votetable['player']   = self._translat_votetable['player'].map(self.real_names)
        self._translat_votetable['voted_by'] = self._translat_votetable['voted_by'].map(self.real_names)

        return self._translat_votetable

    def get_vote_majority(self) -> int:
        '''
        Calculates the amount of votes necessary to reach an absolute majority
        and lynch a player based on the amount of alive players. 

        Parameters: None \n
        Returns: \n
        self._majority (int): The absolute majority of votes required  to lynch a player.
        '''

        if (len(self.player_list) % 2) == 0:
            self._majority = math.ceil(len(self.player_list) / 2) + 1
        else:
            self._majority = math.ceil(len(self.player_list) / 2)
                
        return self._majority