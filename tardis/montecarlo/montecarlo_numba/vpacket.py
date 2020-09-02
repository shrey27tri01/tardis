from numba import float64, int64
from numba import jitclass, njit, gdb

from tardis.montecarlo.montecarlo_numba import njit_dict
from tardis.montecarlo import montecarlo_configuration as montecarlo_configuration

import numpy as np

from tardis.montecarlo.montecarlo_numba.r_packet import (
    calculate_distance_boundary, get_doppler_factor, calculate_distance_line,
    calculate_tau_electron, PacketStatus, move_packet_across_shell_boundary,
    angle_aberration_LF_to_CMF, angle_aberration_CMF_to_LF)

vpacket_spec = [
    ('r', float64),
    ('mu', float64),
    ('nu', float64),
    ('energy', float64),
    ('next_line_id', int64),
    ('current_shell_id', int64),
    ('status', int64),
    ('index', int64)
]

@jitclass(vpacket_spec)
class VPacket(object):
    def __init__(self, r, mu, nu, energy, current_shell_id, next_line_id,
                 index=0):
        self.r = r
        self.mu = mu
        self.nu = nu
        self.energy = energy
        self.current_shell_id = current_shell_id
        self.next_line_id = next_line_id
        self.status = PacketStatus.IN_PROCESS
        self.index = index



@njit(**njit_dict)
def trace_vpacket_within_shell(v_packet, numba_model, numba_plasma,
                               sigma_thomson):
    
    r_inner = numba_model.r_inner[v_packet.current_shell_id]
    r_outer = numba_model.r_outer[v_packet.current_shell_id]

    distance_boundary, delta_shell = calculate_distance_boundary(
        v_packet.r, v_packet.mu, r_inner, r_outer)
    # defining start for line interaction
    start_line_id = v_packet.next_line_id

    # e scattering initialization

    cur_electron_density = numba_plasma.electron_density[
        v_packet.current_shell_id]
    tau_electron = calculate_tau_electron(cur_electron_density, 
                                            distance_boundary,
                                            sigma_thomson)
    tau_trace_combined = tau_electron

    # Calculating doppler factor
    doppler_factor = get_doppler_factor(v_packet.r, v_packet.mu,
                                        numba_model.time_explosion)
    comov_nu = v_packet.nu * doppler_factor
    cur_line_id = start_line_id

    for cur_line_id in range(start_line_id, len(numba_plasma.line_list_nu)):
        if tau_trace_combined > 10: ### FIXME ?????
            break

        nu_line = numba_plasma.line_list_nu[cur_line_id]
        tau_trace_line = numba_plasma.tau_sobolev[
            cur_line_id, v_packet.current_shell_id]

        distance_trace_line = calculate_distance_line(
            v_packet, comov_nu, nu_line, numba_model.time_explosion)

        if distance_boundary <= distance_trace_line:
            break
        
        tau_trace_combined += tau_trace_line



    else:
        if cur_line_id == (len(numba_plasma.line_list_nu) - 1):
            cur_line_id += 1
    v_packet.next_line_id = cur_line_id
        
    return tau_trace_combined, distance_boundary, delta_shell

@njit(**njit_dict)
def trace_vpacket(v_packet, numba_model, numba_plasma, sigma_thomson):
    """
    Trace single vpacket.
    Parameters
    ----------
    v_packet
    numba_model
    numba_plasma

    Returns
    -------

    """

    tau_trace_combined = 0.0
    while True:
        tau_trace_combined_shell, distance_boundary, delta_shell = trace_vpacket_within_shell(
            v_packet, numba_model, numba_plasma, sigma_thomson
        )
        tau_trace_combined += tau_trace_combined_shell
        if tau_trace_combined > 10:
            break
        move_packet_across_shell_boundary(v_packet, delta_shell, 
        len(numba_model.r_inner))
        if v_packet.status == PacketStatus.EMITTED:
            break
        
        # Moving the v_packet
        new_r = np.sqrt(v_packet.r**2 + distance_boundary**2 +
                         2.0 * v_packet.r * distance_boundary * v_packet.mu)
        v_packet.mu = (v_packet.mu * v_packet.r + distance_boundary) / new_r
        v_packet.r = new_r
    return tau_trace_combined

@njit(**njit_dict)
def trace_vpacket_volley(r_packet, vpacket_collection, numba_model,
                         numba_plasma, sigma_thomson):
    """
    Shoot a volley of vpackets (the vpacket collection specifies how many) 
    from the current position of the rpacket. 
    
    Parameters
    ----------
    r_packet : [type]
        [description]
    vpacket_collection : [type]
        [description]
    numba_model : [type]
        [description]
    numba_plasma : [type]
        [description]
    """
    
    if ((r_packet.nu < vpacket_collection.spectrum_frequency[0]) or 
        (r_packet.nu > vpacket_collection.spectrum_frequency[-1])):
        
        return

    
    no_of_vpackets = vpacket_collection.number_of_vpackets
    if no_of_vpackets == 0:
        return

    ### TODO theoretical check for r_packet nu within vpackets bins - is done somewhere else I think
    if r_packet.r > numba_model.r_inner[0]: # not on inner_boundary
        mu_min = -np.sqrt(1 - (numba_model.r_inner[0] / r_packet.r) ** 2)
        v_packet_on_inner_boundary = False
        if montecarlo_configuration.full_relativity:
            mu_min = angle_aberration_LF_to_CMF(r_packet,
                                                 numba_model.time_explosion,
                                                 mu_min)
    else:
        v_packet_on_inner_boundary = True
        mu_min = 0.0
    
    mu_bin = (1.0 - mu_min) / no_of_vpackets
    r_packet_doppler_factor = get_doppler_factor(r_packet.r, r_packet.mu, 
                                                 numba_model.time_explosion)
    for i in range(no_of_vpackets):
        v_packet_mu = mu_min + i * mu_bin + np.random.random() * mu_bin

        if v_packet_on_inner_boundary: # The weights are described in K&S 2014
            weight = 2 * v_packet_mu / no_of_vpackets
        else:
            weight = (1 - mu_min) / (2 * no_of_vpackets)

        # C code: next line, angle_aberration_CMF_to_LF( & virt_packet, storage);
        if montecarlo_configuration.full_relativity:
            v_packet_mu = angle_aberration_CMF_to_LF(
                r_packet,
                numba_model.time_explosion,
                v_packet_mu
            )
        v_packet_doppler_factor = get_doppler_factor(
            r_packet.r, v_packet_mu, numba_model.time_explosion)

        # transform between r_packet mu and v_packet_mu

        doppler_factor_ratio = (
            r_packet_doppler_factor / v_packet_doppler_factor)

        v_packet_nu = r_packet.nu * doppler_factor_ratio
        v_packet_energy = r_packet.energy * weight * doppler_factor_ratio

        v_packet = VPacket(r_packet.r, v_packet_mu, v_packet_nu, 
                           v_packet_energy, r_packet.current_shell_id, 
                           r_packet.next_line_id, i)

        tau_vpacket = trace_vpacket(v_packet, numba_model, numba_plasma,
                                    sigma_thomson)
        
        v_packet.energy *= np.exp(-tau_vpacket)

        vpacket_collection.nus[vpacket_collection.idx] = v_packet.nu
        vpacket_collection.energies[vpacket_collection.idx] = v_packet.energy
        vpacket_collection.idx += 1