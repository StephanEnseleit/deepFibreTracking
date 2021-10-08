import os, sys

import gym
from gym.spaces import Discrete, Box
import numpy as np

import dipy.reconst.dti as dti
from dipy.data import get_sphere
from dipy.data import HemiSphere, Sphere
from dipy.core.sphere import disperse_charges
import torch

from scipy.interpolate import RegularGridInterpolator

from dfibert.data.postprocessing import res100, resample
from dfibert.data import HCPDataContainer, ISMRMDataContainer, PointOutsideOfDWIError
from dfibert.tracker import StreamlinesFromFileTracker
from dfibert.util import get_grid

from ._state import TractographyState

import shapely.geometry as geom
from shapely.ops import nearest_points
from shapely.strtree import STRtree


from collections import deque

class RLtractEnvironment(gym.Env):
    def __init__(self, device, stepWidth = 0.8, action_space=20, dataset = '100307', grid_dim = [3,3,3], maxL2dist_to_State = 0.1, pReferenceStreamlines = "data/HCP307200_DTI_smallSet.vtk", tracking_in_RAS = True, fa_threshold = 0.1, b_val = 1000):
        print("Loading precomputed streamlines (%s) for ID %s" % (pReferenceStreamlines, dataset))
        self.device = device
        self.dataset = HCPDataContainer(dataset)
        self.dataset.normalize() #normalize HCP data
        self.dataset.crop(b_val) #select only data corresponding to a certain b-value
        self.dataset.generate_fa() #prepare for interpolation of fa values
        
        self.stepWidth = stepWidth
        self.dtype = torch.FloatTensor # vs. torch.cuda.FloatTensor
        #sphere = HemiSphere(xyz=res)#get_sphere("repulsion100")
        np.random.seed(42)
        
        phi = np.pi * np.random.rand(action_space)
        theta = 2 * np.pi * np.random.rand(action_space)
        sphere = HemiSphere(theta=theta, phi=phi)  #Sphere(theta=theta, phi=phi)
        sphere, potential = disperse_charges(sphere, 5000) # enforce uniform distribtuion of our points
        self.sphere = sphere
        
        ## debug ##
        #self.sphere = get_sphere('repulsion100')
        #print("Repulsion100!")
        self.directions = torch.from_numpy(self.sphere.vertices).to(device)
        noActions, _ = self.directions.shape

        self.action_space = Discrete(noActions) #spaces.Discrete(noActions+1)
        #self.observation_space = Box(low=0, high=150, shape=(2700,))
        self.dwi_postprocessor = resample(sphere=get_sphere('repulsion100'))    #resample(sphere=sphere)
        self.referenceStreamline_ijk = None
        self.grid = get_grid(np.array(grid_dim))
        self.maxL2dist_to_State = maxL2dist_to_State

        self.maxSteps = 2000
        
        ## load streamlines
        self.changeReferenceStreamlinesFile(pReferenceStreamlines, tracking_in_RAS)
        
        obs_shape = self.getObservationFromState(self.state).shape
        
        self.observation_space = Box(low=0, high=150, shape=obs_shape)

        self.max_mean_step_angle = 1.5
        self.max_step_angle = 1.59
        self.max_dist_to_referenceStreamline = 0.5 # => 0.5 average pixel distance
        
        self.fa_threshold = fa_threshold
        
        ## init odf interpolator
        self._init_odf()
        

    def _init_odf(self):
        print("Computing ODF")
        # fit DTI model to data
        dti_model = dti.TensorModel(self.dataset.data.gtab, fit_method='LS')
        dti_fit = dti_model.fit(self.dataset.data.dwi, mask=self.dataset.data.binarymask)

        # compute ODF
        odf = dti_fit.odf(self.sphere)

        ## set up interpolator for odf evaluation
        x_range = np.arange(odf.shape[0])
        y_range = np.arange(odf.shape[1])
        z_range = np.arange(odf.shape[2])
        
        self.odf_interpolator = RegularGridInterpolator((x_range,y_range,z_range), odf)


        
    def changeReferenceStreamlinesFile(self, pReferenceStreamlines, tracking_in_RAS = True):
        self.pReferenceStreamlines = pReferenceStreamlines
        self.tracking_in_RAS = tracking_in_RAS
        
        # grab streamlines from file
        file_sl = StreamlinesFromFileTracker(self.pReferenceStreamlines)
        file_sl.track()
        self.tracked_streamlines = file_sl.get_streamlines()

        # filter streamlines that are shorter than 10 nodes
        self.tracked_streamlines = list(filter(lambda sl: len(sl) >= 10, self.tracked_streamlines))
        
        
        # convert all streamlines of our dataset into IJK & Shapely LineString format
        if(self.tracking_in_RAS):
            self.tracked_streamlines_torch = [torch.FloatTensor(self.dataset.to_ijk(x)).to(self.device) for x in self.tracked_streamlines]
            self.lines = [geom.LineString(self.dataset.to_ijk(line)) for line in self.tracked_streamlines]
        else:
            self.tracked_streamlines_torch = [torch.FloatTensor(x).to(self.device) for x in self.tracked_streamlines]
            self.lines = [geom.LineString(line) for line in self.tracked_streamlines]

        # build search tree to locate nearest streamlines
        self.tree = STRtree(self.lines)
        
        self.reset()
        
        
    def interpolateDWIatState(self, stateCoordinates):       
        #TODO: maybe stay in RAS all the time then no need to transfer to IJK
        ras_points = self.dataset.to_ras(stateCoordinates) # Transform state to World RAS+ coordinate system
        
        ras_points = self.grid + ras_points

        try:
            interpolated_dwi = self.dataset.get_interpolated_dwi(ras_points, postprocessing=self.dwi_postprocessor)
        except:
            #print("Point outside of brain mask :(")
            return None
        interpolated_dwi = np.rollaxis(interpolated_dwi,3) #CxWxHxD
        #interpolated_dwi = self.dtype(interpolated_dwi).to(self.device)
        return interpolated_dwi
    

    

    def step(self, action):
        self.stepCounter += 1

        ### Termination conditions ###
        # I. number of steps larger than maximum
        if (self.stepCounter == self.maxSteps):
            return self.state, 0., True, {} 
        
        # II. fa below threshold? stop tracking
        if (self.dataset.get_fa(self.state.getCoordinate()) < self.fa_threshold):
                #Defi reached the terminal state
                print('_', end='', flush=True)
                return self.state, 0., True, {}    

        ### Tracking ###
        cur_tangent = self.directions[action].view(-1,3)
        cur_position = self.state.getCoordinate().view(-1,3)
        next_position = cur_position + self.stepWidth * cur_tangent
        nextState = TractographyState(next_position, self.interpolateDWIatState)
        
        ### REWARD ###
        # compute reward based on
        # I. cosine distance to peak direction of ODF (=> imitate maximum direction getter)
        odf_peak_dir = self._get_best_action_ODF(cur_position).view(-1,3)
        reward = abs(torch.nn.functional.cosine_similarity(odf_peak_dir, cur_tangent))
        #print("1",reward, self.stepCounter)
        # II. cosine similarity of current tangent to previous tangent (=> prefer going straight)
        if(self.stepCounter > 1):
            prev_tangent = self.state_history[-1].getCoordinate() - self.state_history[-2].getCoordinate()
            prev_tangent = prev_tangent.view(-1,3)
            prev_tangent = prev_tangent / torch.sqrt(torch.sum(prev_tangent**2, dim = 1)) ## normalize to unit vector
            reward = reward * torch.nn.functional.cosine_similarity(prev_tangent, cur_tangent)
        #print("2",reward, self.stepCounter)
        ### book keeping
        self.state_history.append(nextState)
        self.state = nextState
        
        return nextState, reward, False, {}

    
    def _get_multi_best_action(self, current_direction, my_position, K = 3):
        # main peak from ODF
        reward = self._get_multi_best_action_ODF(my_position, K)

        if(current_direction is not None):
            reward = reward * (torch.nn.functional.cosine_similarity(self.directions, current_direction)).view(1,-1)

        reward = torch.max(reward, axis = 0).values
        best_action = torch.argmax(reward)
        print("Max reward: %.2f" % (torch.max(reward).cpu().detach().numpy()))
        return best_action


    def _get_best_action(self, current_direction, my_position):

        # main peak from ODF
        peak_dir = self._get_best_action_ODF(my_position)

        # cosine similarity wrt. all directions
        reward = abs(torch.nn.functional.cosine_similarity(torch.from_numpy(peak_dir).view(1,-1), self.directions))

        if(current_direction is not None):
            reward = reward * (torch.nn.functional.cosine_similarity(self.directions, current_direction))

        best_action = torch.argmax(reward)
        print("Max reward: %.2f" % (torch.max(reward).cpu().detach().numpy()))
        return best_action

    
    def _get_best_action_ODF(self, my_position):
        '''
        ODF computation at 3x3x3 grid
        #coolsl0_odf = odf_interpolator(my_position).squeeze()
        #coord, data = next_state.getCoordinate(), next_state.getValue()
        #grid = get_grid(np.array([3,3,3]))
        ras_points = env.dataset.to_ras(my_position)
        ras_points = grid + ras_points

        interpolated_dwi = env.dataset.get_interpolated_dwi(ras_points, postprocessing=None)

        dti_fit = dti_model.fit(interpolated_dwi)
        coolsl0_odf = dti_fit.odf(sphere)
        coolsl0_odf = np.mean(coolsl0_odf.reshape(-1,100), axis=0)
        '''

        # ODF interpolation
        coolsl0_odf = self.odf_interpolator(my_position).squeeze()

        # maximum direction getter
        best_action = np.argmax(coolsl0_odf)
        peak_dir = self.directions[best_action]
        return peak_dir

    
    def _get_multi_best_action_ODF(self, my_position, K = 3):
        my_odf = self.odf_interpolator(my_position).squeeze()
        k_largest = np.argpartition(my_odf.squeeze(),-K)[-K:]
        peak_dirs_torch = self.directions[k_largest].view(K,3)
        rewards = torch.stack([abs(torch.nn.functional.cosine_similarity(peak_dirs_torch[k:k+1,:], self.directions.view(-1, 3))) for k in range(K)])
        return rewards

    
    def rewardForState(self, state):
        # The reward will be close to one if the agent is 
        # staying close to our reference streamline.
        self.l2_distance = self.minDistToStreamline(self.line, state)
        return 1 - self.l2_distance
 

    def rewardForTerminalState(self, state):
        qry_pt = state.getCoordinate().view(3)
        distance = torch.sum((self.referenceStreamline_ijk[-1,:] - qry_pt)**2)
        return distance

    
    def cosineSimilarity(self, path_1, path_2):
        return torch.nn.functional.cosine_similarity(path_2, path_1, dim=0)

    
    def arccos(self, angle):
        return torch.arccos(angle)

    
    def getObservationFromState(self, state):
        dwi_values = state.getValue().flatten()
        past_coordinates = np.array(list(self.state_history)).flatten()
        return np.concatenate((dwi_values, past_coordinates))

    
    # switch reference streamline of environment
    ##@TODO: optimize runtime
    def switchStreamline(self, streamline):
        self.line = streamline        
        self.referenceStreamline_ijk = self.dtype(np.array(streamline.coords[:])).to(self.device)
        self.step_angles = []


    # reset the game and returns the observed data from the last episode
    def reset(self, streamline_index=None, start_middle_of_streamline = True):              
        if streamline_index == None:
            streamline_index = np.random.randint(len(self.tracked_streamlines))
        
        if(self.tracking_in_RAS):
            referenceStreamline_ras = self.tracked_streamlines[streamline_index]
            referenceStreamline_ijk = self.dataset.to_ijk(referenceStreamline_ras)
        else:
            referenceStreamline_ijk = self.tracked_streamlines[streamline_index]
        
        self.switchStreamline(geom.LineString(referenceStreamline_ijk))
        
        self.done = False
        self.stepCounter = 0
        self.reward = 0
        self.past_reward = 0
        self.points_visited = 1 #position_index
        
        tracking_start_index = 0
        if(start_middle_of_streamline):
            tracking_start_index = len(self.referenceStreamline_ijk) // 2
        
        self.state = TractographyState(self.referenceStreamline_ijk[tracking_start_index], self.interpolateDWIatState)
        self.state_history = deque(maxlen=4)
        while len(self.state_history) != 4:
            self.state_history.append(self.state)
        
        return self.state


    def render(self, mode="human"):
        pass
    
    
    '''
    deprecated: these functions were implimented via Shapely which is not applicable to 3d data.. it is basically
    ignoring the 3rd dimension in any operation
    
    
    def cosineSimtoStreamline(self, state, nextState):
        #current_index = np.min([self.points_visited,len(self.referenceStreamline_ijk)-1])
        current_index = np.min([self.closestStreamlinePoint(self.state) + 1, len(self.referenceStreamline_ijk)-1])
        path_vector = (nextState.getCoordinate() - state.getCoordinate()).squeeze(0)
        reference_vector = self.referenceStreamline_ijk[current_index]-self.referenceStreamline_ijk[current_index-1]
        cosine_sim = torch.nn.functional.cosine_similarity(path_vector, reference_vector, dim=0)
        return cosine_sim
    
    
    def _get_best_action(self):
        with torch.no_grad():
            distances = []

            for i in range(self.action_space.n):
                action_vector = self.directions[i]
                action_vector = self._correct_direction(action_vector)
                positionNextState = self.state.getCoordinate() + self.stepWidth * action_vector

                dist_streamline = torch.mean( (torch.FloatTensor(self.line.coords[:]) - positionNextState)**2, dim = 1 )

                distances.append(torch.min(dist_streamline))

        return np.argmin(distances)
    
    
    def lineDistance(self, state):
        #point = geom.Point(state.getCoordinate())
        #return point.distance(self.line)
        point = geom.Point(self.state.getCoordinate())
        
        # our action should be closest to the optimal point on our streamline
        p_streamline = self.line.interpolate(self.stepCounter * self.stepWidth)
        
        return p_streamline.distance(point)

    
    def closestStreamlinePoint(self, state):
        distances = torch.cdist(torch.FloatTensor([self.line.coords[:]]), state.getCoordinate().unsqueeze(dim=0).float(), p=2,).squeeze(0)
        index = torch.argmin(distances)
        return index
    
    
    def minDistToStreamline(self, streamline, state):
        dist_streamline = torch.mean( (torch.FloatTensor(streamline.coords[:]) - state.getCoordinate())**2, dim = 1 )
        return torch.min(dist_streamline)
    
    
    def _correct_direction(self, action_vector):
        # handle keeping the agent to go in the direction we want
        if self.stepCounter <= 1:                                                               # if no past states
            last_direction = self.referenceStreamline_ijk[1] - self.referenceStreamline_ijk[0]  # get the direction in which the reference steamline is going
        else: # else get the direction the agent has been going so far
            last_direction = self.state_history[-1].getCoordinate() - self.state_history[-2].getCoordinate()

        if np.dot(action_vector, last_direction) < 0: # if the agent chooses the complete opposite direction
            action_vector = -1 * action_vector  # force it to follow the rough direction of the streamline
        return action_vector
    '''
