# -*- coding: utf-8 -*-
"""
Moves a trainer to a Pokemon.

Events:
    move_to_map_pokemon
        When a generic message is logged
        Returns:
            message: Log message.

    move_to_map_pokemon_fail
        When the worker fails.
        Returns:
            message: Failure message.

    move_to_map_pokemon_updated_map
        When worker updates the PokemonGo-Map.
        Returns:
            lat: Latitude
            lon: Longitude

    move_to_map_pokemon_teleport_to
        When trainer is teleported to a Pokemon.
        Returns:
            poke_name: Pokemon's name
            poke_dist: Distance from the trainer
            poke_lat: Latitude of the Pokemon
            poke_lon: Longitude of the Pokemon
            disappears_in: Number of seconds before the Pokemon disappears

    move_to_map_pokemon_encounter
        When a trainer encounters a Pokemon by teleporting or walking.
        Returns:
            poke_name: Pokemon's name
            poke_dist: Distance from the trainer
            poke_lat: Latitude of the Pokemon
            poke_lon: Longitude of the Pokemon
            disappears_in: Number of seconds before the Pokemon disappears

    move_to_map_pokemon_move_towards
        When a trainer moves toward a Pokemon.
        Returns:
            poke_name: Pokemon's name
            poke_dist: Distance from the trainer
            poke_lat: Latitude of the Pokemon
            poke_lon: Longitude of the Pokemon
            disappears_in: Number of seconds before the Pokemon disappears

    move_to_map_pokemon_teleport_back
        When a trainer teleports back to thier previous location.
        Returns:
            last_lat: Trainer's last known latitude
            last_lon: Trainer's last known longitude

"""

import os
import time
import json
import base64
import requests

from pokemongo_bot import inventory
from pokemongo_bot.base_dir import _base_dir
from pokemongo_bot.cell_workers.utils import distance, format_dist, format_time, fort_details
from pokemongo_bot.walkers.walker_factory import walker_factory
from pokemongo_bot.worker_result import WorkerResult
from pokemongo_bot.base_task import BaseTask
from pokemongo_bot.cell_workers.pokemon_catch_worker import PokemonCatchWorker
from random import uniform
from pokemongo_bot.constants import Constants

ULTRABALL_ID = 3
GREATBALL_ID = 2
POKEBALL_ID = 1


class MoveToMapPokemon(BaseTask):
    """Task for moving a trainer to a Pokemon."""
    SUPPORTED_TASK_API_VERSION = 1

    def initialize(self):
        self.last_map_update = 0
        self.pokemon_data = self.bot.pokemon_list
        self.unit = self.bot.config.distance_unit
        self.caught = []
        self.min_ball = self.config.get('min_ball', 1)
        self.map_path = self.config.get('map_path', 'raw_data')
        self.walker = self.config.get('walker', 'StepWalker')
        self.snipe_high_prio_only = self.config.get('snipe_high_prio_only', False)
        self.snipe_high_prio_threshold = self.config.get('snipe_high_prio_threshold', 400)
        self.by_pass_times = 0

        data_file = os.path.join(_base_dir, 'map-caught-{}.json'.format(self.bot.config.username))
        if os.path.isfile(data_file):
            self.caught = json.load(
                open(data_file)
            )
        self.alt = uniform(self.bot.config.alt_min, self.bot.config.alt_max)

    def get_pokemon_from_social(self):
        if not hasattr(self.bot, 'mqtt_pokemon_list'):
            return []
        if not self.bot.mqtt_pokemon_list or len(self.bot.mqtt_pokemon_list) <= 0:
            return []

        pokemon_list = []
        now = int(time.time())
        tmp_pokemon_list = self.bot.mqtt_pokemon_list
        self.bot.mqtt_pokemon_list = []

        for pokemon in tmp_pokemon_list:
            pokemon['encounter_id'] = pokemon['encounter_id']
            pokemon['spawn_point_id'] = pokemon['spawn_point_id']
            pokemon['disappear_time'] = int(pokemon['expiration_timestamp_ms'] / 1000)
            pokemon['name'] = self.pokemon_data[pokemon['pokemon_id'] - 1]['Name']
            pokemon['is_vip'] = pokemon['name'] in self.bot.config.vips

            if pokemon['name'] not in self.config['catch']:
                if self.config.get('debug', False):
                    self._emit_failure("Not catching {}".format(pokemon['name']))
                continue
            else:
                if self.config.get('debug', False):
                    self._emit_log("Catching {}".format(pokemon['name']))

            if self.is_inspected(pokemon):
                continue

            pokemon['priority'] = self.config['catch'].get(pokemon['name'], 0)

            pokemon['dist'] = distance(
                self.bot.position[0],
                self.bot.position[1],
                pokemon['latitude'],
                pokemon['longitude']
            )

            # If distance to pokemon greater than the max_sniping_distance, then ignore regardless of "snipe" setting
            if pokemon['dist'] > self.config.get('max_sniping_distance', 10000):
                continue

            # If distance bigger than walking distance, ignore if sniping is not active
            if pokemon['dist'] > self.config.get('max_walking_distance', 1000) and not self.config.get('snipe', False):
                continue

            # if pokemon not reachable with mean walking speed (by config)
            mean_walk_speed = (self.bot.config.walk_max + self.bot.config.walk_min) / 2
            if pokemon['dist'] > ((pokemon['disappear_time'] - now) * mean_walk_speed) and not self.config['snipe']:
                continue
            pokemon_list.append(pokemon)
        return pokemon_list

    def get_pokemon_from_url(self):
        pokemon_list = []

        try:
            # The controller/action name doesnt need to be split. Instead, use the full URL
            request = requests.get(self.config['address'])
            response = request.json()
        except requests.exceptions.ConnectionError:
            self._emit_failure('Could not get data from {}'.format(self.config['address']))
            return pokemon_list
        except ValueError:
            self._emit_failure('JSON format is not valid')
            return pokemon_list

        time_now = int(time.time())

        for pokemon in response['pokemons']:
            try:
                # Attempt to get potential nullable values
                pokemon['encounter_id'] = "" if not pokemon['encounter_id'] else long(base64.b64decode(pokemon['encounter_id']))
                pokemon['spawn_point_id'] = "" if not pokemon['spawnpoint_id'] else pokemon['spawnpoint_id']
                pokemon['iv'] = 0 if 'iv' not in pokemon else pokemon['iv']

                # Other parsable values...
                pokemon['disappear_time'] = int(pokemon['disappear_time'] / 1000)
                pokemon['name'] = self.pokemon_data[pokemon['pokemon_id'] - 1]['Name']
                pokemon['is_vip'] = pokemon['name'] in self.bot.config.vips
            except:
                self._emit_failure('Error while parsing information')
                continue

            if pokemon['name'] not in self.config['catch'] and not pokemon['is_vip']:
                if self.config.get('debug', False):
                    self._emit_log('Skipped {} because its not in catch list and not a VIP'.format(pokemon['name']))
                continue

            if self.is_inspected(pokemon):
                if self.config.get('debug', False):
                    self._emit_log('Skipped {} because it was already catch or does not exist'.format(pokemon['name']))
                continue

            pokemon['priority'] = self.config['catch'].get(pokemon['name'], 0)
            pokemon['dist'] = distance(
                self.bot.position[0],
                self.bot.position[1],
                pokemon['latitude'],
                pokemon['longitude']
            )

            if pokemon['dist'] > self.config['max_sniping_distance'] and self.config['snipe']:
                if self.config.get('debug', False):
                    self._emit_log(
                        'Skipped {} because the sniping distance exceeds the max ({})'.format(pokemon['name'], self.config['max_sniping_distance']))
                continue

            if pokemon['dist'] > self.config['max_walking_distance'] and not self.config['snipe']:
                if self.config.get('debug', False):
                    self._emit_log(
                        'Skipped {} because the walking distance exceeds the max ({})'.format(pokemon['name'],
                                                                                              self.config[
                                                                                                  'max_walking_distance']))
                continue

            # pokemon not reachable with mean walking speed (by config)
            mean_walk_speed = (self.bot.config.walk_max + self.bot.config.walk_min) / 2
            if pokemon['dist'] > ((pokemon['disappear_time'] - time_now) * mean_walk_speed) and not self.config['snipe']:
                if self.config.get('debug', False):
                    self._emit_log(
                        'Skipped {} because -pokemon not reachable with mean walking speed (by config)-'.format(
                            pokemon['name']))
                continue

            pokemon_list.append(pokemon)

        return pokemon_list

    def is_inspected(self, pokemon):
        for caught_pokemon in self.caught:
            # Since IDs might be invalid (null/blank) by this time, compare by approximate location (TODO: make a better comparision)
            if "{0:.4f}".format(pokemon['latitude']) == "{0:.4f}".format(caught_pokemon['latitude']) and "{0:.4f}".format(pokemon['longitude']) == "{0:.4f}".format(caught_pokemon['longitude']):
                return True
        return False

    # Stores a target so that
    def inspect(self, pokemon):
        # Make sure it was not caught!
        for caught_pokemon in self.caught:
            if "{0:.4f}".format(pokemon['latitude']) == "{0:.4f}".format(caught_pokemon['latitude']) and "{0:.4f}".format(pokemon['longitude']) == "{0:.4f}".format(caught_pokemon['longitude']):
                return
        if len(self.caught) >= 200:
            self.caught.pop(0)
        self.caught.append(pokemon)

    def snipe(self, pokemon):
        # Backup position before anything
        last_position = self.bot.position[0:2]

        # Teleport, so that we can see nearby stuff
        self.bot.heartbeat()
        self._teleport_to(pokemon)

        # Simulate kind of a lag after teleporting/moving to a long distance
        time.sleep(2)

        # If social is enabled, trust it
        exists = self.bot.config.enable_social
        verify = not self.bot.config.enable_social

        # If social is disabled, we will have to make sure the target still exists
        if verify:
            nearby_pokemons = []
            nearby_stuff = self.bot.get_meta_cell()

            # Sleep some time, so that we have accurate results (successfull cell data request)
            time.sleep(2)

            # Retrieve nearby pokemons for validation
            if 'wild_pokemons' in nearby_stuff:
                nearby_pokemons.extend(nearby_stuff['wild_pokemons'])
            if 'catchable_pokemons' in nearby_stuff:
                nearby_pokemons.extend(nearby_stuff['catchable_pokemons'])

            # Make sure the target still/really exists (TODO: validate expiration)
            for nearby_pokemon in nearby_pokemons:
                is_wild = 'pokemon_data' in nearby_pokemon
                nearby_pokemon_id = nearby_pokemon['pokemon_data']['pokemon_id'] if is_wild else nearby_pokemon['pokemon_id']

                if nearby_pokemon_id == pokemon['pokemon_id']:
                    exists = True

                    # Also, if the IDs arent valid, update them!
                    if not pokemon['encounter_id'] or not pokemon['spawnpoint_id']:
                        pokemon['encounter_id'] = nearby_pokemon['encounter_id']
                        pokemon['spawn_point_id'] = nearby_pokemon['spawn_point_id']
                        pokemon['disappear_time'] = nearby_pokemon['last_modified_timestamp_ms'] if is_wild else nearby_pokemon['expiration_timestamp_ms']
                    break

        # If target exists, catch it, otherwise ignore
        if exists:
            self._encountered(pokemon)
            catch_worker = PokemonCatchWorker(pokemon, self.bot, self.config)
            api_encounter_response = catch_worker.create_encounter_api_call()
            time.sleep(self.config.get('snipe_sleep_sec', 2))
            self._teleport_back(last_position)
            self.bot.api.set_position(last_position[0], last_position[1], self.alt, False)
            time.sleep(self.config.get('snipe_sleep_sec', 2))
            self.bot.heartbeat()
            catch_worker.work(api_encounter_response)
            self.inspect(pokemon)
            return WorkerResult.SUCCESS
        else:
            self._emit_failure('{} doesnt exist anymore. Skipping...'.format(pokemon['name']))
            self.inspect(pokemon)
            time.sleep(self.config.get('snipe_sleep_sec', 2))
            self._teleport_back(last_position)
            self.bot.api.set_position(last_position[0], last_position[1], self.alt, False)
            time.sleep(self.config.get('snipe_sleep_sec', 2))
            return WorkerResult.SUCCESS

    def dump_caught_pokemon(self):
        user_data_map_caught = os.path.join(_base_dir, 'data', 'map-caught-{}.json'.format(self.bot.config.username))
        with open(user_data_map_caught, 'w') as outfile:
            json.dump(self.caught, outfile)

    def work(self):
        # check for pokeballs (excluding masterball)
        pokeballs_quantity = inventory.items().get(POKEBALL_ID).count
        superballs_quantity = inventory.items().get(GREATBALL_ID).count
        ultraballs_quantity = inventory.items().get(ULTRABALL_ID).count

        # Validate the balls quantity
        if (pokeballs_quantity + superballs_quantity + ultraballs_quantity) < self.min_ball:
            if self.config.get('debug', False):
                self._emit_log("Not enough balls to start sniping (have {}, {} needed)".format(
                    pokeballs_quantity + superballs_quantity + ultraballs_quantity, self.min_ball))
            return WorkerResult.SUCCESS

        # Retrieve pokemos
        self.dump_caught_pokemon()
        if self.bot.config.enable_social:
            if self.config['snipe']:
                self.by_pass_times = self.by_pass_times + 1
                if self.by_pass_times < self.config.get('skip_rounds', 30):
                    if self.config.get('debug', False):
                        self._emit_log("Skipping pass {}".format(self.by_pass_times))
                    return WorkerResult.SUCCESS
                self.by_pass_times = 0
            pokemon_list = self.get_pokemon_from_social()
        else:
            pokemon_list = self.get_pokemon_from_url()

        # TODO: check if its really working
        if self.config['mode'] == 'priority':
            pokemon_list.sort(key=lambda x: x['priority'], reverse=True)
        if self.config['prioritize_vips']:
            pokemon_list.sort(key=lambda x: x['is_vip'], reverse=True)
        #pokemon_list.sort(key=lambda x: x['dist'])

        if len(pokemon_list) < 1:
            if self.config.get('debug', False):
                self._emit_log("No pokemons in list to snipe")
            return WorkerResult.SUCCESS

        pokemon = pokemon_list[0]
        if self.config.get('debug', False):
            self._emit_log('How many pokemon in list: {}'.format(len(pokemon_list)))
        if self.config['snipe']:
            if self.snipe_high_prio_only:
                count = 0
                for pokemon in pokemon_list:
                    if self.snipe_high_prio_threshold < pokemon['priority']:
                        self.snipe(pokemon)
                        count = count + 1
                        if count >= self.config.get('snipe_max_in_chain', 2):
                            return WorkerResult.SUCCESS
                        if count is not 1:
                            time.sleep(self.config.get('snipe_sleep_sec', 2) * 5)
                    else:
                        if self.config.get('debug', False):
                            self._emit_log('this pokemon is not good enough to snipe {}'.format(pokemon))
                return WorkerResult.SUCCESS
            else:
                return self.snipe(pokemon)
            return WorkerResult.SUCCESS

        # check for pokeballs (excluding masterball)
        # checking again as we may have lost some if we sniped
        pokeballs_quantity = inventory.items().get(POKEBALL_ID).count
        superballs_quantity = inventory.items().get(GREATBALL_ID).count
        ultraballs_quantity = inventory.items().get(ULTRABALL_ID).count

        if pokeballs_quantity + superballs_quantity + ultraballs_quantity < self.min_ball:
            return WorkerResult.SUCCESS

        nearest_fort = self.get_nearest_fort_on_the_way(pokemon)

        if pokemon['is_vip'] or nearest_fort is None:
            # lock catching(with pokemon_id specified) while moving to vip pokemon or no fort around
            self.bot.capture_locked = pokemon['pokemon_id']
            step_walker = self._move_to(pokemon)
            if not step_walker.step():

                if pokemon['dist'] < Constants.MAX_DISTANCE_POKEMON_IS_REACHABLE:
                    self._encountered(pokemon)
                    self.bot.capture_locked = False  # unlock catch_worker
                    self.inspect(pokemon)
                    return WorkerResult.SUCCESS
                else:
                    return WorkerResult.RUNNING

        else:
            step_walker = self._move_to_pokemon_througt_fort(nearest_fort, pokemon)
            if not step_walker or not step_walker.step():
                return WorkerResult.RUNNING

    def _emit_failure(self, msg):
        self.emit_event(
            'move_to_map_pokemon_fail',
            formatted='Failure! {message}',
            data={'message': msg}
        )

    def _emit_log(self, msg):
        self.emit_event(
            'move_to_map_pokemon',
            formatted='{message}',
            data={'message': msg}
        )

    def _pokemon_event_data(self, pokemon):
        """Generates parameters used for the Bot's event manager.

        Args:
            pokemon: Pokemon object

        Returns:
            Dictionary with Pokemon's info.
        """
        now = int(time.time())
        return {
            'poke_name': pokemon['name'],
            'poke_dist': (format_dist(pokemon['dist'], self.unit)),
            'poke_lat': pokemon['latitude'],
            'poke_lon': pokemon['longitude'],
            'disappears_in': (format_time(pokemon['disappear_time'] - now))
        }

    def _teleport_to(self, pokemon):
        self.emit_event(
            'move_to_map_pokemon_teleport_to',
            formatted='Teleporting to {poke_name}. ({poke_dist})',
            data=self._pokemon_event_data(pokemon)
        )
        self.bot.api.set_position(pokemon['latitude'], pokemon['longitude'], self.alt, True)

    def _encountered(self, pokemon):
        self.emit_event(
            'move_to_map_pokemon_encounter',
            formatted='Encountered Pokemon: {poke_name}',
            data=self._pokemon_event_data(pokemon)
        )

    def _teleport_back(self, last_position):
        self.emit_event(
            'move_to_map_pokemon_teleport_back',
            formatted=('Teleporting back to previous location ({last_lat}, {last_lon})...'),
            data={'last_lat': last_position[0], 'last_lon': last_position[1]}
        )

    def _move_to(self, pokemon):
        """Moves trainer towards a Pokemon.

        Args:
            pokemon: Pokemon to move to.

        Returns:
            Walker
        """
        now = int(time.time())
        self.emit_event(
            'move_to_map_pokemon_move_towards',
            formatted=('Moving towards {poke_name}, {poke_dist}, left ('
                       '{disappears_in})'),
            data=self._pokemon_event_data(pokemon)
        )
        return walker_factory(self.walker,
                              self.bot,
                              pokemon['latitude'],
                              pokemon['longitude']
                              )

    def _move_to_pokemon_througt_fort(self, fort, pokemon):
        """Moves trainer towards a fort before a Pokemon.

        Args:
            fort

        Returns:
            StepWalker
        """

        nearest_fort = fort

        lat = nearest_fort['latitude']
        lng = nearest_fort['longitude']
        fortID = nearest_fort['id']
        details = fort_details(self.bot, fortID, lat, lng)
        fort_name = details.get('name', 'Unknown')

        unit = self.bot.config.distance_unit  # Unit to use when printing formatted distance

        dist = distance(
            self.bot.position[0],
            self.bot.position[1],
            lat,
            lng
        )

        if dist > Constants.MAX_DISTANCE_FORT_IS_REACHABLE:
            pokemon_throught_fort_event_data = {
                'fort_name': u"{}".format(fort_name),
                'distance': format_dist(dist, unit),
                'poke_name': pokemon['name'],
                'poke_dist': (format_dist(pokemon['dist'], self.unit))
            }

            self.emit_event(
                'moving_to_pokemon_throught_fort',
                formatted="Moving towards {poke_name} - {poke_dist}  through pokestop  {fort_name} - {distance}",
                data=pokemon_throught_fort_event_data
            )
        else:
            self.emit_event(
                'arrived_at_fort',
                formatted='Arrived at fort.'
            )

        return walker_factory(self.walker,
                              self.bot,
                              lat,
                              lng
                              )

    def get_nearest_fort_on_the_way(self, pokemon):
        forts = self.bot.get_forts(order_by_distance=True)

        # Remove stops that are still on timeout
        forts = filter(lambda x: x["id"] not in self.bot.fort_timeouts, forts)
        i = 0
        while i < len(forts):
            ratio = float(self.config.get('max_extra_dist_fort', 20))
            dist_self_to_fort = distance(self.bot.position[0], self.bot.position[1], forts[i]['latitude'],
                                         forts[i]['longitude'])
            dist_fort_to_pokemon = distance(pokemon['latitude'], pokemon['longitude'], forts[i]['latitude'],
                                            forts[i]['longitude'])
            total_dist = dist_self_to_fort + dist_fort_to_pokemon
            dist_self_to_pokemon = distance(self.bot.position[0], self.bot.position[1], pokemon['latitude'],
                                            pokemon['longitude'])
            if total_dist < (1 + (ratio / 100)) * dist_self_to_pokemon:
                i = i + 1
            else:
                del forts[i]
            # Return nearest fort if there are remaining
        if len(forts) > 0:
            return forts[0]
        else:
            return None
